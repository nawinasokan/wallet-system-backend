import json
from dataclasses import dataclass
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder


class ApiError(Exception):
    """Raised for failures that must roll the whole DB transaction back (auth, 404, bad input)."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def body(self) -> dict:
        err: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            err["details"] = jsonable(self.details)
        return {"error": err}


@dataclass
class Result:
    """Business outcome that is *returned* (not raised) so side effects recorded on the way
    (risk decisions) commit, and so it can be stored against an idempotency key."""

    status: int
    body: Any


def jsonable(obj: Any) -> Any:
    """Plain-JSON view of a body (datetimes -> ISO strings, UUIDs -> str). Money is already str."""
    return json.loads(json.dumps(obj, cls=DjangoJSONEncoder))


def ok(status: int, body: Any) -> Result:
    return Result(status, jsonable(body))


def reject(status: int, code: str, message: str, details: dict | None = None) -> Result:
    return Result(status, ApiError(status, code, message, details).body())
