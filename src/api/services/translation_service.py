"""兼容旧入口，任务用例正式实现位于 application。"""

import sys

from ...application.use_cases import translation_tasks

sys.modules[__name__] = translation_tasks
