"""
LLM 模型工厂
"""

from __future__ import annotations

from langchain_core.runnables import RunnableLambda

from ..observability import build_langfuse_callbacks
from .request_budget import RequestBudget

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

    def _traced(self, model, run_name: str):
        """把 Langfuse 回调挂到模型上；未接入时原样返回。"""
        if self._callbacks:
            model = model.with_config({"callbacks": self._callbacks, "run_name": run_name})

        async def invoke(payload, config):
            async with self.budget.request():
                return await model.ainvoke(payload, config=config)

        return RunnableLambda(invoke).with_config({"run_name": run_name})

    def create_translator(self):
        model = _ChatOpenAI(
            model=self.config.model_name,
            temperature=self.config.translator_temperature,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            timeout=float(self.config.get("concurrency.request_timeout", 60)),
            max_retries=0,
        )
        return self._traced(model, "translator")

    def create_checker(self):
        model = _ChatOpenAI(
            model=self.config.model_name,
            temperature=self.config.checker_temperature,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            timeout=float(self.config.get("concurrency.request_timeout", 60)),
            max_retries=0,
        )
        return self._traced(model, "checker")

    def create_analyst(self):
        model = _ChatOpenAI(
            model=self.config.model_name,
            temperature=self.config.analyst_temperature,
            api_key=self.config.api_key,
            base_url=self.config.api_base_url,
            timeout=float(self.config.get("concurrency.request_timeout", 60)),
            max_retries=0,
        )
        return self._traced(model, "analyst")
