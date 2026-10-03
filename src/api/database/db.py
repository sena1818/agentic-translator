"""兼容旧入口，正式存储实现位于 infrastructure。"""

from ...infrastructure.persistence.database import Database

__all__ = ["Database"]
