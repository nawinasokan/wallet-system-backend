import logging

from rest_framework import exceptions as drf_exc
from rest_framework.response import Response

from .errors import ApiError

log = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """One error envelope for the whole API: {"error": {"code", "message", "details?"}}."""
    if isinstance(exc, ApiError):
        return Response(exc.body(), status=exc.status)
    if isinstance(exc, drf_exc.ValidationError):
        return Response(
            {"error": {"code": "validation_error", "message": _flatten(exc.detail)}}, status=400
        )
    if isinstance(exc, drf_exc.ParseError):
        return Response({"error": {"code": "invalid_json", "message": "Malformed JSON"}}, status=400)
    if isinstance(exc, drf_exc.APIException):
        status = exc.status_code
        code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed"}.get(
            status, "bad_request"
        )
        return Response({"error": {"code": code, "message": str(exc.detail)}}, status=status)
    log.exception("unhandled error", exc_info=exc)
    return Response({"error": {"code": "internal_error", "message": "Internal server error"}}, status=500)


def _flatten(detail) -> str:
    if isinstance(detail, dict):
        return "; ".join(f"{k}: {_flatten(v)}" for k, v in detail.items())
    if isinstance(detail, (list, tuple)):
        return ", ".join(_flatten(v) for v in detail)
    return str(detail)
