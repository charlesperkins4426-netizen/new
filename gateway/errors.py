from __future__ import annotations


class GatewayError(Exception):
    """An error safe for API clients and request logs (never pass raw secrets)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        account_id: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.account_id = account_id
        self.retry_after = retry_after

    def payload(self) -> dict:
        error = {"message": self.message, "type": "gateway_error", "code": self.code}
        if self.account_id:
            error["account_id"] = self.account_id
        return {"error": error}

    @property
    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
