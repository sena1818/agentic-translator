"""
修复基准：用注入错误 + 脚本化的假模型，度量段级修复是否"修好一类、新增另一类"。

不需要密钥。流程：
1. 取注入基准自带段对中的纯文字段落拼成一篇文档，术语表同注入基准；
2. 假翻译员给每段注入一种可被规则查出的错误：含术语的段换错术语，含数字的段删掉数字，
   其余段轮流整段不翻、整段漏掉、照抄正确译文；
3. 假审校员知道每段的正确译文，但"手滑"：每隔一次修复，就在修好的译文里夹一个英文词
   （规则质检会报"残留英文单词"）——这正是"修好一类、新增另一类"；
4. 统计最终仍有问题的段、被接受的手滑修复、模型调用次数。

假审校员同时听得懂整段重写和约束式编辑两种回复格式，所以改动前后可以用同一个基准比较。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from pathlib import Path

from .injection import Pair, inject_wrong_term, load_dataset

SIDE_EFFECT = " however"
_SEG_RE = re.compile(r'<seg id="(\d+)">(.*?)</seg>', re.S)
_BLOCK_RE = re.compile(r"【段 (\d+)】\n原文：(.*?)\n当前译文：(.*?)\n问题：", re.S)
_NUMBER_RE = re.compile(r"(?<![0-9A-Za-z_.])\d+(?:\.\d+)?(?![0-9A-Za-z_])")
ERROR_KINDS = ("term", "number", "untranslated", "missing")
REST_CYCLE = ("untranslated", "missing", "none")


def _bench_pairs(dataset: Path) -> tuple[list[Pair], dict[str, str]]:
    data = json.loads(dataset.read_text(encoding="utf-8"))
    pairs, _ = load_dataset(data)
    plain = [p for p in pairs if not re.search(r"[\[`\n]", p.source)]
    return plain, data.get("glossary", {})


def _corrupt(pair: Pair, kind: str) -> str | None:
    """返回假翻译员给这段的输出；None 表示整段漏掉。"""
    if kind == "term":
        case = inject_wrong_term(pair)
        return case.corrupted if case else pair.translation
    if kind == "number":
        match = _NUMBER_RE.search(pair.translation)
        return pair.translation[: match.start()] + pair.translation[match.end() :] if match else pair.translation
    if kind == "untranslated":
        return pair.source
    if kind == "missing":
        return None
    return pair.translation


class ScriptedModels:
    """假翻译员 + 手滑的假审校员；记录调用次数与被注入的手滑。"""

    def __init__(self, pairs: list[Pair]):
        self.correct = {_key(p.source): p.translation for p in pairs}
        self.first_pass = {}
        rest = 0
        for pair in pairs:
            if inject_wrong_term(pair):
                kind = "term"
            elif _NUMBER_RE.search(pair.translation):
                kind = "number"
            else:
                kind = REST_CYCLE[rest % len(REST_CYCLE)]
                rest += 1
            self.first_pass[_key(pair.source)] = (kind, _corrupt(pair, kind))
        self.checker_calls = 0
        self.slips = 0
        self._fixes = 0

    def translator(self, prompt_value) -> str:
        body = prompt_value.to_string().split("【待翻译段落】:")[-1]
        out = []
        for seg_id, text in _SEG_RE.findall(body):
            _kind, output = self.first_pass.get(_key(text), ("none", text))
            if output is not None:
                out.append(f'<seg id="{seg_id}">{output}</seg>')
        return "\n".join(out)

    def _sloppy(self, text: str) -> str:
        self._fixes += 1
        if self._fixes % 2 == 0:
            self.slips += 1
            return text.rstrip("。") + SIDE_EFFECT + "。"
        return text

    def checker(self, prompt_value) -> str:
        self.checker_calls += 1
        prompt = prompt_value.to_string()
        body = prompt.split("【待修复段落】:")[-1]
        wants_edits = "<edit " in prompt
        out = []
        for seg_id, source, current in _BLOCK_RE.findall(body):
            correct = self.correct.get(_key(source))
            if correct is None:
                continue
            current = "" if current.strip() == "（缺失）" else current.strip()
            if wants_edits and current:
                prefix = len(_common_prefix(current, correct))
                suffix = len(_common_prefix(current[prefix:][::-1], correct[prefix:][::-1]))
                prefix = max(0, prefix - 2)  # 按 Prompt 要求带上前后几个字，from 不能为空
                old, new = current[prefix : len(current) - suffix], correct[prefix : len(correct) - suffix]
                fixed = self._sloppy(new) if old else new
                out.append(f'<edit id="{seg_id}"><from>{old}</from><to>{fixed}</to></edit>')
            else:
                out.append(f'<seg id="{seg_id}">{self._sloppy(correct)}</seg>')
        return "\n".join(out)


def _key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _common_prefix(a: str, b: str) -> str:
    size = 0
    while size < min(len(a), len(b)) and a[size] == b[size]:
        size += 1
    return a[:size]


def run(dataset: Path, max_fix_attempts: int = 1) -> dict:
    from langchain_core.runnables import RunnableLambda

    from ..core.translator import TranslationEngine
    from ..infrastructure.cache.translation_cache import TranslationCache
    from ..utils.config_loader import Config

    pairs, glossary = _bench_pairs(dataset)
    models = ScriptedModels(pairs)

    class Factory:
        def create_translator(self):
            return RunnableLambda(models.translator)

        def create_checker(self):
            return RunnableLambda(models.checker)

        def create_analyst(self):
            return RunnableLambda(lambda _p: '{"summary": "修复基准"}')

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "config/config.yaml"))  # 独立实例，不改全局单例
    config.config.setdefault("quality", {})["max_fix_attempts"] = max_fix_attempts
    text = "\n\n".join(pair.source for pair in pairs) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        engine = TranslationEngine(
            glossary=glossary,
            model_factory=Factory(),
            cache=TranslationCache(Path(tmp) / "cache.db"),
            config=config,
        )
        chunks = engine.plan_chunks(text)
        results = asyncio.run(engine.translate_batch(text, Path(tmp) / "out.md", prepared_chunks=chunks))
        stats = getattr(engine.structured_translator, "repair_stats", None)

    joined = "\n".join(result.translation for result in results)
    injected = {kind: 0 for kind in ERROR_KINDS}
    for pair in pairs:
        kind, output = models.first_pass[_key(pair.source)]
        if kind != "none" and output != pair.translation:  # 段对不具备条件时注入不生效，不计数
            injected[kind] += 1
    fixed = sum(1 for pair in pairs if pair.translation in joined)
    severe = [
        issue
        for result in results
        for issue in result.quality_report.get("issues", [])
        if issue["severity"] in {"high", "medium"}
    ]
    return {
        "segments": len(pairs),
        "injected": injected,
        "segments_correct_after_repair": fixed,
        "segments_with_severe_issues": len({issue.get("segment") for issue in severe}),
        "repair_model_calls": models.checker_calls,
        "sloppy_fixes_offered": models.slips,
        "sloppy_fixes_accepted": joined.count(SIDE_EFFECT.strip()),
        "max_fix_attempts": max_fix_attempts,
        **({"repair_stats": stats.as_dict()} if stats is not None else {}),
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(prog="translator-repair-bench", description="修复基准（无需密钥）")
    parser.add_argument("--dataset", type=Path, default=root / "docs/evals/datasets/injection_pairs.json")
    parser.add_argument("--max-fix-attempts", type=int, default=1)
    args = parser.parse_args(argv)
    print(json.dumps(run(args.dataset, args.max_fix_attempts), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
