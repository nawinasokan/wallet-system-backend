import uuid

from django.conf import settings

from ..auth import Actor, assert_can_access
from ..errors import ApiError, Result, ok, reject
from ..models import Wallet
from ..money import m
from ..risk.engine import get_engine
from ..risk.types import RuleContext
from .ledger import lock_wallets, post_journal, spent_today


def transfer(actor: Actor, src_id, dst_id, amount: int, description: str | None = None) -> Result:
    if src_id == dst_id:
        raise ApiError(400, "same_wallet", "Source and destination must differ")

    # Both wallets are locked together in id order: no A->B / B->A deadlock, and from here on the
    # status, balance, daily total and risk counts we read cannot change under us.
    locked = lock_wallets([src_id, dst_id])
    src, dst = locked.get(src_id), locked.get(dst_id)
    if src is None:
        raise ApiError(404, "wallet_not_found", "Source wallet not found")
    assert_can_access(actor, src)
    if dst is None:
        raise ApiError(404, "destination_not_found", "Destination wallet not found")

    if src.currency != dst.currency:
        return reject(422, "currency_mismatch", "Wallets have different currencies")
    if src.status == Wallet.Status.FROZEN:
        return reject(409, "wallet_frozen", "Source wallet is frozen")
    if src.status == Wallet.Status.CLOSED:
        return reject(409, "wallet_closed", "Source wallet is closed")
    if dst.status == Wallet.Status.CLOSED:
        return reject(409, "destination_closed", "Destination wallet is closed")
    if src.balance < amount:
        return reject(422, "insufficient_funds", "Insufficient funds", {"balance": m(src.balance)})

    limit = src.daily_limit if src.daily_limit is not None else settings.DEFAULT_DAILY_LIMIT
    spent = spent_today(src.id)
    if spent + amount > limit:
        return reject(422, "daily_limit_exceeded", "Daily limit exceeded", {"limit": m(limit), "spent_today": m(spent)})

    transfer_id = uuid.uuid4()
    verdict = get_engine().evaluate(RuleContext(src, "transfer", amount), transfer_id)
    if verdict.decision == "block":
        return reject(403, "blocked_by_risk_rule", "Transfer blocked by risk rule", {"rules": verdict.rules_payload()})

    j = post_journal(
        id=transfer_id, type="transfer", amount=amount, source=src, dest=dst, reason=description, created_by=actor.id
    )
    return ok(
        201,
        {
            "transfer_id": j.id,
            "source_wallet_id": src.id,
            "destination_wallet_id": dst.id,
            "amount": m(amount),
            "currency": src.currency,
            "source_balance": m(src.balance - amount),
            "description": description,
            "risk": {"decision": verdict.decision, "rules": verdict.rules_payload()},
            "created_at": j.created_at,
        },
    )
