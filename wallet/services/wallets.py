from django.conf import settings
from django.db import IntegrityError, transaction

from ..auth import Actor, assert_can_access
from ..errors import ApiError
from ..models import Wallet
from ..money import m


def wallet_dto(w: Wallet) -> dict:
    return {
        "id": w.id,
        "owner_id": w.owner_id,
        "currency": w.currency,
        "balance": m(w.balance),
        "status": w.status,
        "status_reason": w.status_reason,
        "daily_limit": m(w.daily_limit),
        "effective_daily_limit": m(w.daily_limit if w.daily_limit is not None else settings.DEFAULT_DAILY_LIMIT),
        "created_at": w.created_at,
    }


def create_wallet(actor: Actor, currency: str, owner_id: str | None = None) -> dict:
    owner = owner_id or actor.id
    if owner != actor.id and not actor.is_admin:
        raise ApiError(403, "forbidden", "Cannot create a wallet for another user")
    if owner.startswith("system:"):
        raise ApiError(400, "invalid_owner", "Reserved owner prefix")
    try:
        with transaction.atomic():
            wallet = Wallet.objects.create(owner_id=owner, currency=currency)
    except IntegrityError:
        raise ApiError(409, "wallet_exists", "A wallet for this user and currency already exists") from None
    return wallet_dto(wallet)


def get_wallet_or_404(wallet_id) -> Wallet:
    w = Wallet.objects.filter(id=wallet_id, kind=Wallet.Kind.USER).first()
    if w is None:
        raise ApiError(404, "wallet_not_found", "Wallet not found")
    return w


def get_wallet(actor: Actor, wallet_id) -> dict:
    w = get_wallet_or_404(wallet_id)
    assert_can_access(actor, w)
    return wallet_dto(w)


def list_wallets(actor: Actor, limit: int = 200) -> list[dict]:
    """A user's own wallets; an admin sees every user wallet. Newest first."""
    qs = Wallet.objects.filter(kind=Wallet.Kind.USER)
    if not actor.is_admin:
        qs = qs.filter(owner_id=actor.id)
    return [wallet_dto(w) for w in qs.order_by("-created_at")[:limit]]
