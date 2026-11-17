"""服务错误类型。

所有业务拒绝均携带稳定的 code 与结构化 details，且不会改动任何状态。
"""


class ServiceError(Exception):
    code = "service_error"

    def __init__(self, message, details=None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(ServiceError):
    code = "validation_error"      # 输入结构或约束非法（种子/基线/来源/修正）


class NotFound(ServiceError):
    code = "not_found"             # 目标未初始化、审定单/冲突不存在


class StaleRevision(ServiceError):
    code = "stale_revision"        # 所操作的审定修订号已过期


class OpenConflicts(ServiceError):
    code = "open_conflicts"        # 仍有未决冲突（交付被阻塞）


class IntegrityViolation(ServiceError):
    code = "integrity_violation"   # 候选存在重复键组/悬空引用等关系违规


class TargetChanged(ServiceError):
    code = "target_changed"        # 目标版本已偏离建单版本，须另建单


class OrderClosed(ServiceError):
    code = "order_closed"          # 已交付单不可修改


class RequestConflict(ServiceError):
    code = "request_conflict"      # 冲突请求（重复单号、重复裁决、回执不匹配等）


class AlreadyInitialized(ServiceError):
    code = "already_initialized"   # 目标数据重复初始化
