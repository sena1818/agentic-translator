"""
阅读遍（可选，默认关闭）：翻译前让模型按章顺序读完全书，产出覆盖全书的文档画像与概念候选。

- 每章一次调用，输入是本章原文与压缩后的累计状态（见 domain/rules/reading_pass.ReadingState，有上界）；
- 每章读完立刻把笔记写入缓存，键由"前一章的键 + 本章原文 + 配置"串成链：
  中断后重跑会命中已读章节、不重复花费；某章原文或配置变了，它和之后的章节重读；
- 某章调用失败只记日志，这一章不贡献笔记、也不写缓存（重跑时再读），阅读继续；
- 单独统计调用次数、提示 token（有用量回报时用实际值，否则粗估）、耗时与命中缓存的章数。

开关：multi_agent.reading_pass.enabled。各阶段的成本见 docs/cost-switches.md。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ...domain.index.book_index import BookIndex
from ...domain.models.translation_models import DocumentProfile
from ...domain.rules.reading_pass import (
    Chapter,
    ChapterNote,
    ReadingLimits,
    ReadingState,
    build_profile,
    concept_terms,
    parse_note,
    split_chapters,
)
from ...infrastructure.llm.retry import ainvoke_with_retries
from ..translate.langchain_compat import ChatPromptTemplate, StrOutputParser
from .term_ledger import estimate_tokens, prepare_sources

logger = logging.getLogger(__name__)

READING_VERSION = "reading-v1"

READING_PROMPT = """你是一名为翻译做准备的研究型读者。你正在按顺序通读一本英文书，现在读到其中一章。
读完后写一份结构化笔记，供之后的翻译统一术语、理解全书脉络。

【到目前为止的阅读笔记】（已压缩）:
{state}

【当前章节】: {chapter_label}
{chapter_text}

【要求】
1. summary：本章讲了什么，不超过 120 字。
2. book_so_far：结合之前的笔记，用不超过 200 字概括到本章为止全书在讲什么。
3. concepts：本章新出现、或被重新定义的概念（作者自造词、专名、被当作术语使用的短语），最多 8 个；
   term 必须是原文里的英文写法；definition 用中文简述作者赋予它的含义；where 写定义所在的小节或一句原话。
   之前笔记里已有、本章没有新含义的不要重复。
4. special_words：被作者赋予特殊含义的常用英语词（如把 hole、dust 当概念使用），最多 5 个；meaning 用中文说明。
5. style_notes：只在第一次注意到时写，文体与语气特点，最多 2 条。
6. 只输出一个 JSON 对象，不要输出其他内容：
{{"summary": "...", "book_so_far": "...", "concepts": [{{"term": "...", "definition": "...", "where": "..."}}],
 "special_words": [{{"word": "...", "meaning": "..."}}], "style_notes": ["..."]}}
"""


@dataclass
class ReadingUsage:
    chapters: int = 0
    calls: int = 0
    cached_chapters: int = 0
    failed_chapters: int = 0
    invalid_notes: int = 0
    prompt_chars: int = 0
    estimated_prompt_tokens: int = 0
    reported_tokens: int = 0  # 服务端用量回报的总 token（输入 + 输出）；没有回报时为 0
    max_state_chars: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {key: (round(value, 2) if isinstance(value, float) else value) for key, value in self.__dict__.items()}


@dataclass
class ReadingResult:
    notes: list[ChapterNote] = field(default_factory=list)
    profile: DocumentProfile = field(default_factory=DocumentProfile.empty)
    concepts: list[tuple[str, int]] = field(default_factory=list)  # (原文写法, 全书次数)，作术语候选第四路
    usage: ReadingUsage = field(default_factory=ReadingUsage)


Getter = Callable[[str], Awaitable[dict | None]]
Setter = Callable[[str, dict], Awaitable[None]]


def reading_limits(config) -> ReadingLimits:
    getter = getattr(config, "get", None) or (lambda _key, default=None: default)
    defaults = ReadingLimits()
    return ReadingLimits(
        min_chapter_chars=int(getter("multi_agent.reading_pass.min_chapter_chars", defaults.min_chapter_chars)),
        max_chapter_chars=int(getter("multi_agent.reading_pass.max_chapter_chars", defaults.max_chapter_chars)),
        max_state_chars=int(getter("multi_agent.reading_pass.max_state_chars", defaults.max_state_chars)),
        profile_max_chars=int(getter("multi_agent.reading_pass.profile_max_chars", defaults.profile_max_chars)),
    )


def reading_enabled(config) -> bool:
    getter = getattr(config, "get", None)
    return bool(getter and getter("multi_agent.enabled", True) and getter("multi_agent.reading_pass.enabled", False))


class ReadingPass:
    def __init__(self, llm, config, load: Getter | None = None, save: Setter | None = None):
        self.llm = llm
        self.config = config
        self.limits = reading_limits(config)
        self.load = load
        self.save = save

    def _get(self, key: str, default=None):
        getter = getattr(self.config, "get", None)
        return getter(key, default) if getter else default

    def chapters(self, text: str, chunks: Sequence | None = None) -> tuple[list[Chapter], BookIndex]:
        index, _ = prepare_sources(text, chunks)
        return split_chapters(index, self.limits), index

    async def run(self, text: str, chunks: Sequence | None = None) -> ReadingResult:
        started = time.monotonic()
        chapters, index = self.chapters(text, chunks)
        prompt = ChatPromptTemplate.from_template(READING_PROMPT)
        state = ReadingState(self.limits)
        usage = ReadingUsage(chapters=len(chapters))
        notes: list[ChapterNote] = []
        key = hashlib.sha256((READING_VERSION + json.dumps(self.limits.__dict__, sort_keys=True)).encode()).hexdigest()
        for chapter in chapters:
            key = hashlib.sha256((key + chapter.title + "\n" + chapter.text).encode()).hexdigest()
            cache_key = "reading:" + key
            saved = await self.load(cache_key) if self.load else None
            if saved is not None:
                note = ChapterNote.from_dict(saved["note"])
                usage.cached_chapters += 1
            else:
                rendered_state = state.render()
                usage.max_state_chars = max(usage.max_state_chars, len(rendered_state))
                payload = {"state": rendered_state, "chapter_label": chapter.label, "chapter_text": chapter.text}
                rendered = prompt.format(**payload)
                usage.calls += 1
                usage.prompt_chars += len(rendered)
                usage.estimated_prompt_tokens += estimate_tokens(rendered)
                try:
                    result, _ = await ainvoke_with_retries(
                        prompt | self.llm, payload, self._get("api.translator.max_retries", 3)
                    )
                except Exception as exc:
                    usage.failed_chapters += 1
                    logger.warning("阅读遍：%s 读取失败，跳过这一章: %s", chapter.label, exc)
                    continue
                raw = result if isinstance(result, str) else StrOutputParser().invoke(result)
                reported = getattr(result, "usage_metadata", None) or {}
                usage.reported_tokens += int(reported.get("total_tokens", 0) or 0)
                note = parse_note(raw, chapter, self.limits)
                if not note.valid:
                    usage.invalid_notes += 1
                    logger.warning("阅读遍：%s 的笔记无法解析，这一章不贡献内容", chapter.label)
                elif self.save:
                    await self.save(cache_key, {"note": note.as_dict()})
            notes.append(note)
            state.update(note)
        usage.max_state_chars = max(usage.max_state_chars, len(state.render()))
        usage.seconds = time.monotonic() - started
        result = ReadingResult(
            notes=notes,
            profile=build_profile(notes, self.limits),
            concepts=concept_terms(notes, index),
            usage=usage,
        )
        logger.info(
            "阅读遍：%s 章（缓存命中 %s，失败 %s），调用 %s 次，提示约 %s token，概念候选 %s 条，耗时 %.1fs",
            usage.chapters,
            usage.cached_chapters,
            usage.failed_chapters,
            usage.calls,
            usage.estimated_prompt_tokens,
            len(result.concepts),
            usage.seconds,
        )
        return result


__all__ = ["ReadingPass", "ReadingResult", "ReadingUsage", "READING_PROMPT", "reading_enabled", "reading_limits"]
