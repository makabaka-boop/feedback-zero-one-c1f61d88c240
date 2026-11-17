"""统一错误模型: 所有非法或冲突操作以 ApiError 抛出, 不产生任何状态变更。"""


class ApiError(Exception):
    """携带 HTTP 状态码与稳定错误码的业务错误。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self):
        err = {"code": self.code, "message": self.message}
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}
