import hmac
import uuid

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from drf_spectacular.utils import extend_schema as _extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from . import serializers as s
from .auth import Actor, require_admin, sign_token
from .bank import sign_callback
from .errors import ApiError, Result
from .idempotency import hash_request, with_idempotency
from .services import admin, reconciliation, statement, topups, transfers, wallets, withdrawals

def extend_schema(**kw):
    """Every endpoint returns a JSON object; document it as such unless a view says otherwise."""
    kw.setdefault("responses", OpenApiTypes.OBJECT)
    return _extend_schema(**kw)


IDEM = OpenApiParameter(
    "Idempotency-Key", str, OpenApiParameter.HEADER, required=True,
    description="Retries with the same key return the original result and never move money twice.",
)


def parse_uuid(value: str):
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ApiError(400, "validation_error", "id must be a UUID") from None


def validated(serializer_cls, data):
    ser = serializer_cls(data=data)
    ser.is_valid(raise_exception=True)
    return ser.validated_data


def send(result: Result, headers: dict | None = None) -> Response:
    return Response(result.body, status=result.status, headers=headers)


def idempotent(request, work) -> Response:
    """Run `work` exactly once per Idempotency-Key and replay the stored result on retries."""
    key = request.headers.get("Idempotency-Key")
    if not key or len(key) > 255:
        raise ApiError(400, "validation_error", "Idempotency-Key header is required (1-255 chars)")
    digest = hash_request(request.method, request.path, request.data)
    result, replayed = with_idempotency(request.user.id, key, digest, work)
    return send(result, {"Idempotent-Replayed": "true"} if replayed else None)


class AdminView(APIView):
    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_admin(request.user)


# ---- auth (dev only) --------------------------------------------------------------------------
class DevTokenView(APIView):
    authentication_classes: list = []

    @extend_schema(tags=["auth"], request=s.DevTokenIn, auth=[],
                   summary="DEV ONLY: mint a JWT for any subject/role (ENABLE_DEV_TOKENS=true)")
    def post(self, request):
        if not settings.ENABLE_DEV_TOKENS:
            raise ApiError(404, "not_found", "Not found")
        d = validated(s.DevTokenIn, request.data)
        return Response({"token": sign_token(Actor(d["sub"], d["role"]))})


class HealthView(APIView):
    authentication_classes: list = []

    @extend_schema(exclude=True)
    def get(self, request):
        return Response({"status": "ok"})


# ---- wallets ----------------------------------------------------------------------------------
class WalletsView(APIView):
    @extend_schema(tags=["wallets"], summary="List my wallets (admins: all wallets)")
    def get(self, request):
        return Response({"items": wallets.list_wallets(request.user)})

    @extend_schema(tags=["wallets"], request=s.CreateWalletIn, summary="Create a wallet (one per user and currency)")
    def post(self, request):
        d = validated(s.CreateWalletIn, request.data)
        return Response(wallets.create_wallet(request.user, d["currency"], d.get("owner_id")), status=201)


class WalletDetailView(APIView):
    @extend_schema(tags=["wallets"], summary="Get wallet and balance")
    def get(self, request, pk):
        return Response(wallets.get_wallet(request.user, parse_uuid(pk)))


class TopUpView(APIView):
    @extend_schema(
        tags=["wallets"], request=s.TopUpIn, parameters=[IDEM], summary="Top up via the mock payment provider",
        description="Payment method `tok_decline` / `tok_insufficient` simulate provider failures; anything else succeeds.",
    )
    def post(self, request, pk):
        wallet_id = parse_uuid(pk)
        d = validated(s.TopUpIn, request.data)
        key = request.headers.get("Idempotency-Key", "")
        return idempotent(
            request, lambda: topups.top_up(request.user, wallet_id, d["amount"], d["payment_method"], key)
        )


class StatementView(APIView):
    @extend_schema(tags=["wallets"], parameters=[s.StatementQuery], summary="Statement: ledger entries, newest first")
    def get(self, request, pk):
        q = validated(s.StatementQuery, request.query_params)
        return Response(
            statement.statement(
                request.user, parse_uuid(pk), date_from=q.get("from_"), date_to=q.get("to"),
                limit=q["limit"], cursor=q.get("cursor"),
            )
        )


# ---- transfers --------------------------------------------------------------------------------
class TransfersView(APIView):
    @extend_schema(tags=["transfers"], request=s.TransferIn, parameters=[IDEM],
                   summary="Atomically move money between two wallets")
    def post(self, request):
        d = validated(s.TransferIn, request.data)
        return idempotent(
            request,
            lambda: transfers.transfer(
                request.user, d["source_wallet_id"], d["destination_wallet_id"], d["amount"], d.get("description")
            ),
        )


# ---- withdrawals ------------------------------------------------------------------------------
class WithdrawalsView(APIView):
    @extend_schema(
        tags=["withdrawals"], request=s.WithdrawalIn, parameters=[IDEM],
        summary="Request a withdrawal to a (mock) bank account",
        description="Funds are reserved immediately. Status: requested → processing → completed | failed "
        "(failed refunds exactly once). Account number suffix picks the mock bank outcome: 0000 fails, "
        "9999 times out, 7777 sends duplicate callbacks, 1111 rejected at submit, otherwise success. Returns 202.",
    )
    def post(self, request, pk):
        wallet_id = parse_uuid(pk)
        d = validated(s.WithdrawalIn, request.data)
        dest = dict(d["destination"])
        return idempotent(request, lambda: withdrawals.request_withdrawal(request.user, wallet_id, d["amount"], dest))


class WithdrawalDetailView(APIView):
    @extend_schema(tags=["withdrawals"], summary="Get withdrawal status")
    def get(self, request, pk):
        return Response(withdrawals.get_withdrawal(request.user, parse_uuid(pk)))


class BankCallbackView(APIView):
    authentication_classes: list = []

    @extend_schema(
        tags=["bank"], request=s.BankCallbackIn, auth=[], summary="Bank webhook: confirm or reject a withdrawal",
        description="Authenticated with `X-Bank-Signature` = hex HMAC-SHA256 of the raw body. Idempotent on "
        "`event_id`; duplicates and late callbacks are safe.",
    )
    def post(self, request):
        raw = request.body  # the HMAC is over the exact bytes received
        expected = sign_callback(settings.BANK_WEBHOOK_SECRET, raw)
        if not hmac.compare_digest(request.headers.get("X-Bank-Signature", ""), expected):
            raise ApiError(401, "invalid_signature", "Bad webhook signature")
        d = validated(s.BankCallbackIn, request.data)
        payload = {**d, "withdrawal_id": str(d["withdrawal_id"])}
        return send(withdrawals.apply_bank_callback(payload))


# ---- admin ------------------------------------------------------------------------------------
class WalletActionView(AdminView):
    action = ""

    @extend_schema(tags=["admin"], request=s.ReasonIn, summary="Change wallet state (reason required, audited)")
    def post(self, request, pk):
        d = validated(s.ReasonIn, request.data)
        return send(admin.change_wallet_state(request.user, parse_uuid(pk), self.action, d["reason"]))


class FreezeView(WalletActionView):
    action = "freeze"


class UnfreezeView(WalletActionView):
    action = "unfreeze"


class CloseView(WalletActionView):
    action = "close"


class StateHistoryView(AdminView):
    @extend_schema(tags=["admin"], summary="Audit history of wallet state changes")
    def get(self, request, pk):
        return Response({"items": admin.state_history(parse_uuid(pk))})


class DailyLimitView(AdminView):
    @extend_schema(tags=["admin"], request=s.DailyLimitIn,
                   summary="Set (or clear with null) the per-wallet daily outgoing limit")
    def put(self, request, pk):
        d = validated(s.DailyLimitIn, request.data)
        return Response(admin.set_daily_limit(parse_uuid(pk), d["daily_limit"]))


class ReverseTransferView(AdminView):
    @extend_schema(tags=["admin"], request=s.ReasonIn, summary="Reverse a transfer with compensating ledger entries")
    def post(self, request, pk):
        d = validated(s.ReasonIn, request.data)
        return send(admin.reverse_transfer(request.user, parse_uuid(pk), d["reason"]))


class FlagsView(AdminView):
    @extend_schema(tags=["admin"], parameters=[s.FlagsQuery],
                   summary="Flagged (and optionally blocked) transfers/withdrawals with the triggering rule")
    def get(self, request):
        q = validated(s.FlagsQuery, request.query_params)
        return Response(admin.list_decisions(q["decision"], q["limit"], q.get("cursor")))


class ReconciliationView(AdminView):
    @extend_schema(tags=["admin"], summary="Verify balances against the ledger and that debits equal credits")
    def get(self, request):
        return Response(reconciliation.reconcile())
