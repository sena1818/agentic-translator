import asyncio

import pytest
from langchain_core.runnables import RunnableLambda

from src.infrastructure.llm import retry as retry_module
from src.infrastructure.llm.rate_limits import QuotaExhaustedError
from src.infrastructure.llm.retry import ainvoke_with_retries


class _StatusError(Exception):
    def __init__(self, status_code: int, message: str = "error"):
        super().__init__(message)
        self.status_code = status_code


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(retry_module.asyncio, "sleep", fake_sleep)
    return waits


def _flaky(errors):
    calls = {"n": 0}

    def run(_payload):
        calls["n"] += 1
        if errors:
            raise errors.pop(0)
        return "ok"

    return RunnableLambda(run), calls


def test_transient_server_error_is_retried():
    chain, calls = _flaky([_StatusError(503, "IteraCompute service is temporarily unavailable")])
    raw, retries = asyncio.run(ainvoke_with_retries(chain, {}, max_retries=2))
    assert (raw, retries, calls["n"]) == ("ok", 1, 2)


def test_client_error_is_not_retried():
    chain, calls = _flaky([_StatusError(400, "bad request")])
    with pytest.raises(_StatusError):
        asyncio.run(ainvoke_with_retries(chain, {}, max_retries=3))
    assert calls["n"] == 1


def test_quota_exhaustion_is_not_retried():
    chain, calls = _flaky([QuotaExhaustedError("all models exhausted")])
    with pytest.raises(QuotaExhaustedError):
        asyncio.run(ainvoke_with_retries(chain, {}, max_retries=3))
    assert calls["n"] == 1


def test_rate_limit_waits_for_server_hint(no_sleep):
    chain, _ = _flaky([_StatusError(429, "Error code: 429 - Please retry in 7s")])
    asyncio.run(ainvoke_with_retries(chain, {}, max_retries=1))
    assert no_sleep == [8]


def test_gives_up_after_max_retries():
    chain, calls = _flaky([_StatusError(503), _StatusError(503)])
    with pytest.raises(_StatusError):
        asyncio.run(ainvoke_with_retries(chain, {}, max_retries=1))
    assert calls["n"] == 2


def test_before_attempt_runs_for_every_attempt():
    chain, _ = _flaky([_StatusError(502)])
    seen = []

    async def before():
        seen.append(1)

    asyncio.run(ainvoke_with_retries(chain, {}, max_retries=2, before_attempt=before))
    assert len(seen) == 2


def test_document_analyzer_survives_one_transient_failure():
    from types import SimpleNamespace

    from src.pipelines.translate.document_analyzer import DocumentAnalyzer
    from src.pipelines.translate.prompt_builder import TranslationPromptBuilder

    errors = [_StatusError(503, "temporarily unavailable")]

    def analyst(_prompt):
        if errors:
            raise errors.pop(0)
        return '{"summary": "一份测试文档"}'

    config = SimpleNamespace(
        multi_agent_enabled=True,
        analyst_max_sections=4,
        analyst_max_chars=2000,
        analyst_max_term_hints=4,
        get=lambda key, default=None: {"api.translator.max_retries": 2}.get(key, default),
    )
    analyzer = DocumentAnalyzer(RunnableLambda(analyst), config, TranslationPromptBuilder(config))
    profile = asyncio.run(analyzer.analyze("Some text.", []))
    assert profile.summary == "一份测试文档"
