"""
LLM 模型工厂
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableLambda

from ..observability import build_langfuse_callbacks
from .rate_limits import QuotaExhaustedError, classify
from .request_budget import RequestBudget

logger = logging.getLogger(__name__)

try:
    from langchain_openai import ChatOpenAI as _ChatOpenAI
except ImportError:  # pragma: no cover - 允许轻量测试环境导入

    class _ChatOpenAI:  # type: ignore
        def __init__(self, *args, **kwargs):
            raise ImportError("langchain_openai 未安装，无法初始化聊天模型")


class ChatModelFactory:
    """统一创建项目所需的聊天模型。"""

    def __init__(self, config):
        self.config = config
        # 开启且具备密钥时接入 Langfuse，否则为空列表（无操作）。
        # 回调只构造一次，三个角色共用，追踪自动经 LangChain 回调链下沉到每次调用。
        self._callbacks = build_langfuse_callbacks(config)
        self.budget = RequestBudget(config)
        # 主模型 + 备用模型：免费层按模型分别计每日额度，用完一个自动换下一个（全角色共享）
        self.model_names = [config.model_name, *(config.get("api.fallback_models") or [])]
        self.exhausted: set[str] = set()

    def _traced(self, models: dict, run_name: str):
        """挂上 Langfuse 回调与共享请求预算；每日额度用完时按顺序切换备用模型。"""
        if self._callbacks:
            models = {
                name: model.with_config({"callbacks": self._callbacks, "run_name": run_name})
                for name, model in models.items()
            }

        async def invoke(payload, config):
            last_error = None
            for name in self.model_names:
                if name in self.exhausted:
                    continue
                try:
                    async with self.budget.request():
                        return await models[name].ainvoke(payload, config=config)
                except Exception as exc:
                    kind, _ = classify(exc)
                    if kind != "daily":
                        raise
                    self.exhausted.add(name)
                    last_error = exc
                    logger.warning("模型 %s 今日额度已用完，切换备用模型", name)
            raise QuotaExhaustedError(
                f"所有模型（{', '.join(self.model_names)}）的每日额度都已用完；"
                "已完成的块已缓存，次日或更换密钥后重新运行即可断点续跑"
            ) from last_error

        return RunnableLambda(invoke).with_config({"run_name": run_name})

    def _models(self, temperature: float) -> dict:
        return {name: self._model(temperature, name) for name in self.model_names}

    def _model(self, temperature: float, model_name: str | None = None):
        options = {
            "model": model_name or self.config.model_name,
            "temperature": temperature,
            "api_key": self.config.api_key,
            "base_url": self.config.api_base_url,
            "timeout": float(self.config.get("concurrency.request_timeout", 60)),
            "max_retries": 0,
        }
        # 推理模型（如 Gemini 3.x Flash、OpenAI o 系列）可压低思考预算，翻译场景下明显更快更省
        reasoning_effort = self.config.get("api.reasoning_effort")
        if reasoning_effort:
            options["reasoning_effort"] = reasoning_effort
        return _ChatOpenAI(**options)

    def create_translator(self):
        return self._traced(self._models(self.config.translator_temperature), "translator")

    def create_checker(self):
        return self._traced(self._models(self.config.checker_temperature), "checker")

    def create_analyst(self):
        return self._traced(self._models(self.config.analyst_temperature), "analyst")
