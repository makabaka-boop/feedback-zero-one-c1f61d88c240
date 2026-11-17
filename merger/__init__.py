"""研究数据修订合并与审定交付系统(纯后端, 仅依赖 Python 标准库 + SQLite)。"""

from .errors import ApiError
from .service import MergeService
from .server import make_server

__all__ = ["ApiError", "MergeService", "make_server"]
__version__ = "1.0.0"
