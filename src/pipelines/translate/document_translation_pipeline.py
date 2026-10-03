"""兼容旧导入路径，翻译用例已统一到 application。"""

from ...application.use_cases.run_translation_pipeline import ProgressCallback, RunTranslationPipeline
from ...domain.models.translation_models import TranslationPipelineOutput

DocumentTranslationPipeline = RunTranslationPipeline
__all__ = ["DocumentTranslationPipeline", "TranslationPipelineOutput", "ProgressCallback"]
