"""
阅读遍的纯逻辑：分章、解析章节笔记、压缩累计状态、汇成文档画像与概念候选。不调用模型、不读写文件。

阅读遍让模型按顺序逐章读完全书，每章写一份结构化笔记（本章摘要、新出现的概念及定义位置、
被赋予特殊含义的常用词），读下一章时只带上压缩后的累计状态。这里负责其中所有确定性的部分：

- split_chapters：按全书索引的章节路径分章，过短的相邻章节合并，过长的章节切成几部分；
- parse_note：容错解析模型返回的笔记 JSON；
- ReadingState：累计状态。渲染给模型看的文本有字符上界，整本书读完也不会超过；
- build_profile：把各章笔记汇成覆盖全书的文档画像，每个条目带章节出处；
- concept_terms：把笔记里的概念与特殊用词汇成术语候选（第四路来源 "reading"）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..index.book_index import BookIndex, term_pattern
from ..models.translation_models import DocumentProfile


@dataclass(frozen=True)
class Chapter:
    """阅读遍的一个阅读单位：一章、几个合并的短章，或一个长章的一部分。"""

    number: int  # 从 1 开始的阅读顺序
    title: str
    text: str
    positions: tuple[int, int]  # 覆盖的全书段位置区间 [首, 尾]

    @property
    def label(self) -> str:
        return f"第 {self.number} 章《{self.title}》" if self.title else f"第 {self.number} 章"


@dataclass
class ConceptNote:
    term: str
    definition: str = ""
    where: str = ""  # 模型给出的定义位置提示（本章内的小节或一句原话）


@dataclass
class SpecialWord:
    word: str
    meaning: str = ""


@dataclass
class ChapterNote:
    chapter: int
    title: str
    summary: str = ""
    book_so_far: str = ""  # 模型维护的"到目前为止全书在讲什么"，最后一章的即全书摘要
    concepts: list[ConceptNote] = field(default_factory=list)
    special_words: list[SpecialWord] = field(default_factory=list)
    style_notes: list[str] = field(default_factory=list)
    valid: bool = True  # 模型输出无法解析时为 False，只记录不中断

    @property
    def label(self) -> str:
        return f"第 {self.chapter} 章《{self.title}》" if self.title else f"第 {self.chapter} 章"

    def as_dict(self) -> dict:
        return {
            "chapter": self.chapter,
            "title": self.title,
            "summary": self.summary,
            "book_so_far": self.book_so_far,
            "concepts": [c.__dict__ for c in self.concepts],
            "special_words": [w.__dict__ for w in self.special_words],
            "style_notes": self.style_notes,
            "valid": self.valid,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ChapterNote:
        return cls(
            chapter=int(data.get("chapter", 0)),
            title=str(data.get("title", "")),
            summary=str(data.get("summary", "")),
            book_so_far=str(data.get("book_so_far", "")),
            concepts=[ConceptNote(**item) for item in data.get("concepts", [])],
            special_words=[SpecialWord(**item) for item in data.get("special_words", [])],
            style_notes=list(data.get("style_notes", [])),
            valid=bool(data.get("valid", True)),
        )


@dataclass(frozen=True)
class ReadingLimits:
    min_chapter_chars: int = 3000  # 短于此的相邻章节合并读
    max_chapter_chars: int = 24000  # 长于此的章节切成几部分（约 6k token）
    max_concepts_per_chapter: int = 8
    max_special_per_chapter: int = 5
    summary_chars: int = 160
    definition_chars: int = 90
    # 累计状态（带入下一章的部分）的上界
    state_recent_chapters: int = 3  # 最近几章的摘要全文保留
    state_older_chapters: int = 12  # 更早的章节只留一行短摘要，最多这么多条
    state_older_chars: int = 40
    state_concepts: int = 30
    state_special_words: int = 10
    state_book_chars: int = 300
    max_state_chars: int = 4000  # 渲染文本的硬上限
    # 汇成的文档画像会进每一块的翻译提示，整段提示文本有上界（与首中尾采样画像约 1k 字符相当）
    profile_max_chars: int = 1200
    profile_term_hints: int = 20


# --------------------------------------------------------------------------- #
# 分章
# --------------------------------------------------------------------------- #


def _heading_depth(index: BookIndex) -> int:
    """取能把全书分成至少 3 组的最浅章节层级；没有标题时返回 -1（按长度切）。"""
    for depth in range(3):
        groups = set()
        for seg in index.segments:
            if len(seg.section_path) > depth:
                groups.add(seg.section_path[: depth + 1])
        if len(groups) >= 3:
            return depth
    return -1


def split_chapters(index: BookIndex, limits: ReadingLimits | None = None) -> list[Chapter]:
    """按章节路径把全书分成阅读单位：连续同章节的段为一组；过短的相邻组合并，过长的切开。"""
    limits = limits or ReadingLimits()
    depth = _heading_depth(index)
    runs: list[tuple[str, list]] = []
    for seg in index.segments:
        if not seg.source.strip():
            continue
        key = seg.section_path[depth] if depth >= 0 and len(seg.section_path) > depth else ""
        if not runs or runs[-1][0] != key:
            runs.append((key, []))
        runs[-1][1].append(seg)

    merged: list[tuple[str, list]] = []
    for title, segments in runs:
        previous = sum(len(s.source) for s in merged[-1][1]) if merged else 0
        if merged and previous < limits.min_chapter_chars:
            prev_title, prev_segments = merged[-1]
            current = sum(len(s.source) for s in segments)
            # 合并后沿用篇幅较大那一组的标题
            keep = (prev_title or title) if previous >= current else (title or prev_title)
            merged[-1] = (keep, prev_segments + segments)
        else:
            merged.append((title, list(segments)))

    chapters: list[Chapter] = []
    for title, segments in merged:
        parts: list[list] = [[]]
        for seg in segments:
            if parts[-1] and sum(len(s.source) for s in parts[-1]) + len(seg.source) > limits.max_chapter_chars:
                parts.append([])
            parts[-1].append(seg)
        for i, part in enumerate(parts):
            name = _clean_title(title)
            if len(parts) > 1:
                name = f"{name}（第 {i + 1}/{len(parts)} 部分）" if name else f"第 {i + 1}/{len(parts)} 部分"
            chapters.append(
                Chapter(
                    number=len(chapters) + 1,
                    title=name,
                    text="\n".join(seg.source for seg in part),
                    positions=(part[0].position, part[-1].position),
                )
            )
    return chapters


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()[:60]


# --------------------------------------------------------------------------- #
# 笔记解析
# --------------------------------------------------------------------------- #


def _clip(text, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_note(raw: str, chapter: Chapter, limits: ReadingLimits | None = None) -> ChapterNote:
    """解析模型返回的章节笔记；无法解析时返回 valid=False 的空笔记（该章不贡献内容，阅读继续）。"""
    limits = limits or ReadingLimits()
    try:
        payload = raw.strip()
        if not payload.startswith("{"):
            match = re.search(r"\{[\s\S]*\}", payload)
            payload = match.group(0) if match else payload
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError("不是 JSON 对象")
    except Exception:
        return ChapterNote(chapter.number, chapter.title, valid=False)

    concepts = []
    for item in data.get("concepts", []) or []:
        if isinstance(item, dict) and str(item.get("term", "")).strip():
            concepts.append(
                ConceptNote(
                    term=_clip(item["term"], 60),
                    definition=_clip(item.get("definition", ""), limits.definition_chars),
                    where=_clip(item.get("where", ""), 60),
                )
            )
    special = []
    for item in data.get("special_words", []) or []:
        if isinstance(item, dict) and str(item.get("word", "")).strip():
            special.append(
                SpecialWord(_clip(item["word"], 40), _clip(item.get("meaning", ""), limits.definition_chars))
            )
    return ChapterNote(
        chapter=chapter.number,
        title=chapter.title,
        summary=_clip(data.get("summary", ""), limits.summary_chars),
        book_so_far=_clip(data.get("book_so_far", ""), limits.state_book_chars),
        concepts=concepts[: limits.max_concepts_per_chapter],
        special_words=special[: limits.max_special_per_chapter],
        style_notes=[_clip(s, 60) for s in data.get("style_notes", []) or [] if str(s).strip()][:3],
    )


# --------------------------------------------------------------------------- #
# 累计状态
# --------------------------------------------------------------------------- #


@dataclass
class _ConceptEntry:
    term: str
    definition: str
    first: str  # 首次出现的章节标签
    order: int
    mentions: int = 1


class ReadingState:
    """带入下一章的压缩状态：最近几章摘要全文、更早章节的短摘要、按提及次数排序的概念表。

    状态只由确定性规则压缩（不额外调用模型），render() 的长度有上界，与已读章节数无关。
    """

    def __init__(self, limits: ReadingLimits | None = None):
        self.limits = limits or ReadingLimits()
        self.book_so_far = ""
        self.summaries: list[tuple[str, str]] = []
        self.concepts: dict[str, _ConceptEntry] = {}
        self.special: dict[str, _ConceptEntry] = {}
        self._order = 0

    def update(self, note: ChapterNote) -> None:
        if not note.valid:
            return
        if note.book_so_far:
            self.book_so_far = note.book_so_far
        self.summaries.append((note.label, note.summary))
        for concept in note.concepts:
            self._remember(self.concepts, concept.term, concept.definition, note.label)
        for word in note.special_words:
            self._remember(self.special, word.word, word.meaning, note.label)
        self._prune()

    def _remember(self, table: dict, term: str, definition: str, label: str) -> None:
        key = term.lower()
        entry = table.get(key)
        if entry is None:
            self._order += 1
            table[key] = _ConceptEntry(term, definition, label, self._order)
        else:
            entry.mentions += 1
            if not entry.definition and definition:
                entry.definition = definition

    def _prune(self) -> None:
        """只保留有界的部分：摘要保留最近 recent+older 条，概念表保留排序靠前的若干条。"""
        keep = self.limits.state_recent_chapters + self.limits.state_older_chapters
        self.summaries = self.summaries[-keep:]
        self.concepts = _top(self.concepts, self.limits.state_concepts * 2)
        self.special = _top(self.special, self.limits.state_special_words * 2)

    def render(self) -> str:
        limits = self.limits
        lines: list[str] = []
        if self.book_so_far:
            lines.append("到目前为止全书在讲：" + _clip(self.book_so_far, limits.state_book_chars))
        recent = self.summaries[-limits.state_recent_chapters :]
        older = self.summaries[: -limits.state_recent_chapters] if len(self.summaries) > len(recent) else []
        if older:
            lines.append("更早的章节：")
            lines += [f"- {label}：{_clip(summary, limits.state_older_chars)}" for label, summary in older]
        if recent:
            lines.append("最近几章：")
            lines += [f"- {label}：{summary}" for label, summary in recent]
        concepts = list(_top(self.concepts, limits.state_concepts).values())
        if concepts:
            lines.append("已出现的概念（首次出现处）：")
            lines += [f"- {c.term}：{_clip(c.definition, 50)}（{c.first}）" for c in concepts]
        special = list(_top(self.special, limits.state_special_words).values())
        if special:
            lines.append("被赋予特殊含义的常用词：")
            lines += [f"- {w.term}：{_clip(w.definition, 40)}" for w in special]
        text = "\n".join(lines) or "（尚未读过任何章节）"
        return text if len(text) <= limits.max_state_chars else text[: limits.max_state_chars - 1] + "…"


def _top(table: dict[str, _ConceptEntry], limit: int) -> dict[str, _ConceptEntry]:
    ranked = sorted(table.items(), key=lambda item: (-item[1].mentions, item[1].order))[:limit]
    return dict(sorted(ranked, key=lambda item: item[1].order))


# --------------------------------------------------------------------------- #
# 产出：文档画像与概念候选
# --------------------------------------------------------------------------- #


def build_profile(notes: list[ChapterNote], limits: ReadingLimits | None = None) -> DocumentProfile:
    """把各章笔记汇成覆盖全书的文档画像；章节概览与术语提示每条都带章节出处。

    画像会进每一块的翻译提示，所以整段提示文本（to_prompt_text）不超过 profile_max_chars：
    摘要与文体说明先占，章节概览最多占四成五，余下按顺序放术语提示，放不下的提示丢弃。
    """
    limits = limits or ReadingLimits()
    valid = [note for note in notes if note.valid]
    if not valid:
        return DocumentProfile.empty()
    summary = _clip(next((note.book_so_far for note in reversed(valid) if note.book_so_far), ""), 200)
    style: list[str] = []
    for note in valid:
        for item in note.style_notes:
            if item not in style:
                style.append(item)
    style = style[:2]

    overview = _overview(valid, int(limits.profile_max_chars * 0.45))

    seen: dict[str, tuple[ConceptNote, str, int]] = {}
    for note in valid:
        for concept in note.concepts:
            key = concept.term.lower()
            if key in seen:
                c, label, mentions = seen[key]
                seen[key] = (c, label, mentions + 1)
            else:
                seen[key] = (concept, note.label, 1)
    ranked = sorted(seen.values(), key=lambda item: -item[2])[: limits.profile_term_hints]
    order = {key: i for i, key in enumerate(seen)}
    ranked.sort(key=lambda item: order[item[0].term.lower()])
    profile = DocumentProfile(summary=summary, style_notes=style, section_overview=overview)
    for c, label, _ in ranked:
        hint = f"{c.term}：{_clip(c.definition, 40)}（首见{label}）"
        profile.terminology_hints.append(hint)
        if len(profile.to_prompt_text()) > limits.profile_max_chars:
            profile.terminology_hints.pop()
            break
    return profile


def _overview(notes: list[ChapterNote], budget: int, min_line: int = 40) -> list[str]:
    """每章一行"出处：摘要"；章数多到放不下时把相邻章节合成一行（出处写成"第 a–b 章"），总长不超过 budget。"""
    notes = [note for note in notes if note.summary]
    if not notes:
        return []
    max_lines = max(1, budget // min_line)
    size = -(-len(notes) // max_lines)  # 每行合几章（向上取整）
    groups = [notes[i : i + size] for i in range(0, len(notes), size)]
    lines = []
    for group in groups:
        label = group[0].label if len(group) == 1 else f"第 {group[0].chapter}–{group[-1].chapter} 章"
        room = max(8, budget // len(groups) - len(label) - 2)
        lines.append(f"{label}：{_clip('；'.join(note.summary for note in group), room)}")
    return lines


def concept_terms(notes: list[ChapterNote], index: BookIndex) -> list[tuple[str, int]]:
    """笔记里的概念与特殊用词 → (原文写法, 全书出现次数)，只保留在原文里真实出现的，按首次被记下的顺序。

    模型可能把概念写成中文或改写过的说法；在原文里找不到的不作为候选。
    """
    corpus = "\n".join(seg.source for seg in index.segments)
    found: dict[str, tuple[str, int]] = {}
    for note in notes:
        if not note.valid:
            continue
        for term in [c.term for c in note.concepts] + [w.word for w in note.special_words]:
            term = term.strip().strip("“”\"'‘’*_")
            key = term.lower()
            if not term or key in found or not re.search(r"[A-Za-z]", term):
                continue
            count = len(term_pattern(term).findall(corpus))
            if count:
                found[key] = (term, count)
    return list(found.values())
