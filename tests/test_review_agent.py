"""审校 agent：分诊规则、四条运行路径（修复生效 / 修复被拒后转人工 / 驳回误报 / 超预算降级）、
关闭时与现状一致、分诊基准与任务流程。模型全部是脚本化的假模型，不需要密钥。"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from src.application.tools.triage import review_registry
from src.domain.review.triage import (
    DISMISSED,
    FIXED,
    HANDOFF,
    CheckedIssue,
    FlaggedIssue,
    ReviewEdit,
    annotate_report,
    apply_review_edits,
    flagged_from_records,
    judge_fix,
    needs_direct_handoff,
    parse_edits,
)
from src.infrastructure.agent import AgentBudget, Checkpoint, ModelReply, ScriptedModel
from src.infrastructure.document import pandoc
from src.pipelines.review.review_agent import ReviewAgent, ReviewAgentConfig, untriaged
from src.pipelines.review.workspace import ReviewWorkspace

needs_pandoc = pytest.mark.skipif(not pandoc.pandoc_available(), reason="需要 pandoc")

NEGATION = ("The method does not improve accuracy.", "该方法提高了准确率。")
SOLID = ("She made a solid argument for the reform.", "她为这项改革提出了扎实的论证。")
GLOSSARY = {"Solid": "固态 (Solid)"}


def _tool(name, args=None):
    return json.dumps({"action": "tool", "tool": name, "args": args or {}}, ensure_ascii=False)


def _submit(conclusion, evidence=("E1",)):
    return json.dumps({"action": "submit", "conclusion": conclusion, "evidence": list(evidence)}, ensure_ascii=False)


def _handoff(reason):
    return json.dumps({"action": "handoff", "reason": reason}, ensure_ascii=False)


def _workspace(pairs, glossary=None):
    workspace = ReviewWorkspace.from_pairs(pairs, glossary or {}, [])
    flagged = []
    for seg_id, (_source, translation) in enumerate(pairs, 1):
        for issue in workspace.check(seg_id, translation):
            if issue.severity in {"high", "medium"}:
                flagged.append(
                    FlaggedIssue(f"q{len(flagged) + 1}", seg_id, seg_id - 1, issue.kind, issue.severity, issue.message)
                )
                break
    workspace.flagged = flagged
    return workspace


# --------------------------------------------------------------------------- #
# 纯规则
# --------------------------------------------------------------------------- #


def test_edits_must_be_local_unique_and_tag_free():
    text = "该方法提高了准确率，准确率很重要。"
    assert apply_review_edits(text, [ReviewEdit("提高了", "没有提高")]) == ("该方法没有提高准确率，准确率很重要。", [])
    _, problems = apply_review_edits(text, [ReviewEdit("准确率", "精度")])
    assert "出现多次" in problems[0]
    _, problems = apply_review_edits(text, [ReviewEdit("提高了", "<b1>提高了</b1>")])
    assert "标签" in problems[0]
    _, problems = apply_review_edits(text, [ReviewEdit("该方法提高了准确率", "x" * 60)])
    assert "超过" in problems[0]
    # 任何一条不合法就整体不应用
    unchanged, problems = apply_review_edits(text, [ReviewEdit("提高了", "没有提高"), ReviewEdit("不存在", "x")])
    assert unchanged == text and problems
    assert parse_edits([{"from": "a"}])[1] and parse_edits([])[1]


def test_fix_takes_effect_only_if_target_goes_away_without_new_issues():
    target = FlaggedIssue("q1", 1, 0, "semantic", "medium", "否定丢失")
    before = [CheckedIssue("semantic", "medium", "否定丢失")]
    assert judge_fix(target, before, []) == (True, "")
    assert judge_fix(target, before, before)[0] is False
    new = [CheckedIssue("fidelity", "medium", "缺少原文数字: 3")]
    ok, reason = judge_fix(target, before, new)
    assert not ok and reason.startswith("新增问题")
    assert judge_fix(target, before, [CheckedIssue("fidelity", "low", "提示")])[0] is True  # low 不阻止


def test_issues_that_edits_cannot_fix_go_straight_to_a_human():
    assert needs_direct_handoff(FlaggedIssue("q", 1, 0, "missing", "high", "模型输出缺少该段"))
    assert needs_direct_handoff(FlaggedIssue("a", 1, 0, "audit:link", "high", "链接地址变了"))
    assert not needs_direct_handoff(FlaggedIssue("q", 1, 0, "terminology", "medium", "术语"))
    assert not needs_direct_handoff(FlaggedIssue("a", 1, 0, "audit:reference", "medium", "编号"))


def test_flagged_issues_come_from_reports_and_translation_side_audit_findings():
    records = [
        {
            "chunk_index": 0,
            "quality_report": {
                "issues": [
                    {"kind": "terminology", "severity": "medium", "message": "[段 1] 术语 x", "segment": 3},
                    {"kind": "repair", "severity": "low", "message": "修复未采用", "segment": 3},
                    {"kind": "semantic", "severity": "medium", "message": "[段 2] [待人工] 否定", "segment": 4},
                    {"kind": "fidelity", "severity": "medium", "message": "块级问题"},
                ]
            },
        }
    ]
    audit = {
        "findings": [
            {"kind": "link", "severity": "high", "origin": "translation", "message": "链接变了", "segment": 3},
            {"kind": "reference", "severity": "medium", "origin": "source", "message": "原文死链", "segment": 3},
        ]
    }
    flagged = flagged_from_records(records, audit)
    assert [(f.seg_id, f.kind, f.message) for f in flagged] == [
        (3, "terminology", "术语 x"),
        (3, "audit:link", "链接变了"),
    ]
    assert flagged[1].chunk_index == 0 and flagged[1].origin == "audit"


# --------------------------------------------------------------------------- #
# 运行路径
# --------------------------------------------------------------------------- #


def test_accepted_fix_is_applied_and_counted():
    workspace = _workspace([NEGATION])
    edit = {"from": "提高了", "to": "没有提高"}
    model = ScriptedModel(
        [
            _tool("get_issue"),
            _tool("check_fix", {"edits": [edit]}),
            _submit({"verdict": "fix", "edits": [edit]}, ["E2"]),
        ]
    )
    run = asyncio.run(ReviewAgent(model, review_registry).triage(workspace))
    [result] = run.results
    assert result.verdict == FIXED and result.after == "该方法没有提高准确率。"
    assert workspace.translation(1) == "该方法没有提高准确率。"
    assert run.summary["by_verdict"] == {FIXED: 1, DISMISSED: 0, HANDOFF: 0}
    assert result.steps == 3 and result.tool_calls == 2 and result.tokens > 0


def test_rejected_fix_never_takes_effect_and_ends_with_a_handoff():
    workspace = _workspace([NEGATION])
    bad = {"from": "该方法", "to": "这种方法"}  # 目标问题还在
    model = ScriptedModel(
        [_tool("get_issue"), _submit({"verdict": "fix", "edits": [bad]}), _handoff("改不好，交给人工")]
    )
    run = asyncio.run(ReviewAgent(model, review_registry).triage(workspace))
    [result] = run.results
    assert result.verdict == HANDOFF and "改不好" in result.reason
    assert workspace.translation(1) == NEGATION[1]
    # 第二步的提交被拒绝，拒绝原因作为观察返回给了模型
    assert "逐类型不退步" in model.calls[2][-1]["content"]


def test_false_positive_is_dismissed_with_a_reason():
    workspace = _workspace([SOLID], GLOSSARY)
    model = ScriptedModel(
        [
            _tool("get_issue"),
            _tool("glossary_lookup", {"term": "solid"}),
            _submit({"verdict": "dismiss", "reason": "好"}, ["E1"]),  # 理由太短，被拒
            _submit({"verdict": "dismiss", "reason": "solid 在这里是形容词'扎实的'，不是术语"}, ["E1", "E2"]),
        ]
    )
    run = asyncio.run(ReviewAgent(model, review_registry).triage(workspace))
    [result] = run.results
    assert result.verdict == DISMISSED and "形容词" in result.reason
    assert workspace.translation(1) == SOLID[1]


def test_budget_exhaustion_degrades_to_a_handoff():
    workspace = _workspace([NEGATION])
    replies = [_tool("get_context", {"radius": r}) for r in (1, 2, 3)] + [_tool("get_issue")] * 5
    config = ReviewAgentConfig(enabled=True, budget=AgentBudget(max_steps=3, max_tokens=50_000, max_tool_calls=10))
    run = asyncio.run(ReviewAgent(ScriptedModel(replies), review_registry, config).triage(workspace))
    [result] = run.results
    assert result.verdict == HANDOFF and result.reason == "budget:steps" and result.steps == 3
    assert run.summary["max_steps_per_issue"] <= 3


def test_direct_handoffs_and_issue_cap_cost_no_model_calls():
    workspace = _workspace([NEGATION, SOLID], GLOSSARY)
    workspace.flagged.insert(0, FlaggedIssue("m", 1, 0, "missing", "high", "模型输出缺少该段"))
    model = ScriptedModel([_tool("get_issue"), _handoff("拿不准")])
    config = ReviewAgentConfig(enabled=True, max_issues=1)
    run = asyncio.run(ReviewAgent(model, review_registry, config).triage(workspace))
    assert [(r.verdict, r.by) for r in run.results] == [(HANDOFF, "rule"), (HANDOFF, "agent"), (HANDOFF, "limit")]
    assert len(model.calls) == 2  # 只有交给 agent 的那一条调用了模型


def test_disabled_agent_leaves_everything_for_a_human_at_zero_cost():
    workspace = _workspace([NEGATION, SOLID], GLOSSARY)
    run = untriaged(workspace)
    assert {r.verdict for r in run.results} == {HANDOFF}
    assert run.summary["tokens"] == 0 and run.summary["agent_runs"] == 0
    assert workspace.translation(1) == NEGATION[1]
    assert ReviewAgentConfig.from_config(None).enabled is False


def test_rerun_resumes_from_the_checkpoint(tmp_path):
    checkpoint = Checkpoint(tmp_path / "review.jsonl")
    edit = {"from": "提高了", "to": "没有提高"}
    first = ScriptedModel([_tool("get_issue"), _submit({"verdict": "fix", "edits": [edit]})])
    asyncio.run(ReviewAgent(first, review_registry).triage(_workspace([NEGATION]), checkpoint=checkpoint))
    again = ScriptedModel([])
    workspace = _workspace([NEGATION])
    run = asyncio.run(ReviewAgent(again, review_registry).triage(workspace, checkpoint=checkpoint))
    assert run.results[0].verdict == FIXED and again.calls == []
    assert workspace.translation(1) == "该方法没有提高准确率。"


def test_annotated_report_removes_fixed_and_marks_the_rest():
    issues = [
        FlaggedIssue("q1", 1, 0, "semantic", "medium", "否定丢失"),
        FlaggedIssue("q2", 2, 0, "terminology", "medium", "术语 Solid"),
        FlaggedIssue("q3", 3, 0, "fidelity", "medium", "缺少原文数字: 3"),
    ]
    from src.domain.review.triage import TriageResult

    results = [
        TriageResult(issues[0], FIXED, edits=[ReviewEdit("a", "b")], after="x"),
        TriageResult(issues[1], DISMISSED, "普通形容词"),
        TriageResult(issues[2], HANDOFF, "agent:拿不准"),
    ]
    report = {
        "passed": False,
        "issues": [
            {"kind": "semantic", "severity": "medium", "message": "[段 1] 否定丢失", "segment": 1},
            {"kind": "terminology", "severity": "medium", "message": "[段 2] 术语 Solid", "segment": 2},
            {"kind": "fidelity", "severity": "medium", "message": "[段 3] 缺少原文数字: 3", "segment": 3},
        ],
    }
    annotated = annotate_report(report, results)
    messages = [(i["severity"], i["message"]) for i in annotated["issues"]]
    assert messages == [
        ("low", "[段 2] [已驳回] 术语 Solid（理由：普通形容词）"),
        ("medium", "[段 3] [待人工] 缺少原文数字: 3（agent:拿不准）"),
    ]
    assert annotated["passed"] is False and len(annotated["review"]) == 3
    # 再收集时，已分诊过的问题不会重复进入
    assert flagged_from_records([{"chunk_index": 0, "quality_report": annotated}]) == []


# --------------------------------------------------------------------------- #
# 分诊基准
# --------------------------------------------------------------------------- #


def test_review_bench_reports_triage_rates_and_every_applied_fix_passes_the_gate():
    from src.evaluation import review_bench

    root = Path(__file__).resolve().parents[1]
    with_audit = pandoc.pandoc_available()
    cases, glossary, segments, stats = review_bench.build_cases(root, with_audit)
    assert sum(c.truth == "dismiss" for c in cases) == 11 and sum(c.truth == "fix" for c in cases) >= 30
    config = ReviewAgentConfig(enabled=True)
    baseline = asyncio.run(review_bench.run_policy(cases, glossary, segments, None, with_audit, config))
    assert baseline["rates"]["handoff"] == 1.0 and baseline["cost"]["tokens"] == 0
    scripted = review_bench.ScriptedReviewer(cases, glossary)
    result = asyncio.run(review_bench.run_policy(cases, glossary, segments, scripted, with_audit, config))
    assert result["gate"]["all_applied_fixes_pass_no_regression"] is True
    assert result["gate"]["rejected_submissions"] > 0  # 没修好的提交被闸门拦下
    assert result["outcome"]["wrong_dismiss"] == 0 and result["outcome"]["correct_dismiss"] > 0
    assert result["rates"]["correct_fix"] > 0.5
    assert result["cost"]["max_tokens_per_issue"] <= config.budget.max_tokens


def test_task_registry_reuses_the_shared_book_tools():
    from src.application.tools.triage import SESSION_TOOLS, TASK_TOOLS
    from src.pipelines.review.session import ReviewSession

    session = ReviewSession(_workspace([NEGATION]))
    assert review_registry(session, service=object()).names() == [*SESSION_TOOLS, *TASK_TOOLS]
    assert review_registry(session).names() == [*SESSION_TOOLS, "book_renderings"]


def test_minimal_edit_reproduces_the_reference():
    from src.evaluation.review_bench import minimal_edit

    for current, reference in [("该方法提高了准确率。", "该方法没有提高准确率。"), ("从 80 到 70。", "从 70 到 80。")]:
        edit = minimal_edit(current, reference)
        text, problems = apply_review_edits(current, [ReviewEdit(edit["from"], edit["to"])])
        assert problems == [] and text == reference


# --------------------------------------------------------------------------- #
# 任务流程
# --------------------------------------------------------------------------- #

DOC = "# Notes\n\nThe Numogram maps the decimal numbers.\n\nNothing was lost in the fire.\n"
TRANSLATIONS = {
    "Notes": "笔记",
    "The Numogram maps the decimal numbers.": "数字图映射十进制数。",  # 术语没按表译
    "Nothing was lost in the fire.": "火灾中一切都保住了。",  # 语义启发式误报：否定改写成肯定
}


class _Factory:
    def create_translator(self):
        import re

        def translate(prompt_value):
            body = prompt_value.to_string().split("【待翻译段落】:")[-1]
            return "\n".join(
                f'<seg id="{i}">{TRANSLATIONS.get(src, src)}</seg>'
                for i, src in re.findall(r'<seg id="(\d+)">(.*?)</seg>', body, re.S)
            )

        return RunnableLambda(translate)

    def create_checker(self):
        return RunnableLambda(lambda prompt: "")  # 分类型修复不起作用，问题留到审校 agent

    def create_analyst(self):
        return RunnableLambda(lambda prompt: '{"summary": "测试"}')


class _Reviewer:
    """按问题内容决定动作的假审校员：Numogram 用编辑修，否定误报驳回。"""

    async def __call__(self, messages, *, metadata=None):
        task = messages[1]["content"]
        step = metadata["step"]
        if "Numogram" in task:
            task_id = task.split("task_id：", 1)[1].split("；", 1)[0]
            edit = {"from": "数字图映射", "to": "数图映射"}
            script = [
                _tool("get_issue"),
                _tool("term_renderings", {"task_id": task_id, "term": "Numogram"}),  # 共享注册表里的全书工具
                _submit({"verdict": "fix", "edits": [edit]}, ["E1", "E2"]),
            ]
        else:
            script = [_tool("get_issue"), _submit({"verdict": "dismiss", "reason": "否定改写为肯定，意思相同"})]
        return ModelReply(script[min(step, len(script)) - 1])


@needs_pandoc
def test_task_triage_writes_fixes_and_verdicts_back_and_is_off_by_default(tmp_path, monkeypatch):
    from src.application.use_cases import translation_tasks
    from src.core.translator import TranslationEngine
    from src.infrastructure.cache.translation_cache import TranslationCache
    from src.infrastructure.filesystem.task_files import TaskFiles
    from src.infrastructure.persistence.database import Database
    from src.infrastructure.persistence.glossary_repository import GlossaryService
    from src.infrastructure.persistence.task_repository import TaskRepository
    from src.utils.config_loader import get_config

    base = get_config(Path("config/config.yaml"))

    def engine_factory(glossary=None, config=None):
        return TranslationEngine(
            glossary=glossary,
            config=config,
            model_factory=_Factory(),
            cache=TranslationCache(tmp_path / "cache.db"),
            engine="native",
        )

    monkeypatch.setattr(translation_tasks, "TranslationEngine", engine_factory)
    glossaries = GlossaryService()
    glossaries.glossary_dir = tmp_path / "glossaries"
    glossaries.glossary_dir.mkdir()
    (glossaries.glossary_dir / "g.json").write_text(json.dumps({"Numogram": "数图 (Numogram)"}), encoding="utf-8")

    async def make_service(enabled):
        config = copy.copy(base)
        config.config = copy.deepcopy(base.config)
        config.config["review_agent"]["enabled"] = enabled
        config.config["quality"]["semantic_check"]["enabled"] = True  # 语义启发式（不花 token）会报那条误报
        db = Database(str(tmp_path / f"tasks-{enabled}.db"))
        await db.initialize()
        service = translation_tasks.TranslationService(
            repository=TaskRepository(db),
            files=TaskFiles(tmp_path / f"data-{enabled}"),
            config=config,
            glossary_service=glossaries,
        )
        service.review.model_builder = lambda _config: _Reviewer()
        task = await service.create_task(DOC.encode(), "notes.md", "g")
        await service.start_translation(task.task_id)
        return service, task.task_id

    async def run():
        # 关闭时：不能运行，任务结果不变
        off, off_id = await make_service(False)
        assert (await off.review_agent_status(off_id)) == {"enabled": False, "last_run": None}
        page = await off.get_chunks(off_id)
        with pytest.raises(ValueError, match="未启用"):
            await off.run_review_agent(off_id, page["revision"])
        assert (await off.get_chunks(off_id))["chunks"] == page["chunks"]

        on, task_id = await make_service(True)
        page = await on.get_chunks(task_id)
        kinds = {i["kind"] for i in page["chunks"][0]["quality_report"]["issues"] if i["severity"] == "medium"}
        assert {"terminology", "semantic"} <= kinds
        saved = await on.run_review_agent(task_id, page["revision"])
        assert saved["summary"]["by_verdict"] == {FIXED: 1, DISMISSED: 1, HANDOFF: 0}
        assert saved["summary"]["max_steps_per_issue"] <= 8

        after = await on.get_chunks(task_id)
        assert after["revision"] != page["revision"]
        [record] = after["chunks"]
        assert "数图映射十进制数" in record["translation"] and "一切都保住了" in record["translation"]
        report = record["quality_report"]
        severe = [i for i in report["issues"] if i["severity"] in {"high", "medium"}]
        assert severe == [] and report["passed"] is True
        assert any("[已驳回]" in i["message"] for i in report["issues"])
        assert {r["verdict"] for r in report["review"]} == {FIXED, DISMISSED}
        assert (await on.review_agent_status(task_id))["last_run"]["summary"]["issues"] == 2
        trace = [
            json.loads(line) for line in on.review._dir().joinpath(f"{task_id}.trace.jsonl").read_text().splitlines()
        ]
        assert any(e["type"] == "tool" and e["tool"] == "term_renderings" and e["ok"] for e in trace)

    asyncio.run(run())
