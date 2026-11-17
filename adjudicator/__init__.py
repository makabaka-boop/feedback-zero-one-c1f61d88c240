"""研究数据修订合并审定服务（纯后端，Python 标准库 + SQLite）。

两个团队的独立修订按共同基线做三方合并，冲突人工裁决，
关系校验通过后在目标版本未变的前提下原子交付。
"""
from .service import MergeService
from . import errors

__all__ = ["MergeService", "errors"]
__version__ = "1.0.0"
