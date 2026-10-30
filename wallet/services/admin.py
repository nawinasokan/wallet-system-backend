from django.db import connection, transaction

from ..auth import Actor
from ..errors import ApiError, Result, ok, reject
from ..models import RiskDecision, Transaction, Wallet, WalletStateHistory, Withdrawal
from ..money import m
from .ledger import lock_wallets, post_journal
from .wallets import wallet_dto

# Allowed state machine. `closed` is terminal.
TRANSITIONS = {
    "freeze": (["active"], "frozen"),
    "unfreeze": (["frozen"], "active"),
    "close": (["active", "frozen"], "closed"),
}


def change_wallet_state(actor: Actor, wallet_id, action: str, reason: str) -> Result:
    """Change wallet state under the wallet row lock. Transfers/withdrawals lock the same row before
    reading `status`, so a freeze either lands before an in-flight transfer (which then sees `frozen`
    and is rejected) or after it has fully committed. There is no in-between."""
    with transaction.atomic():
        wallet = lock_wallets([wallet_id]).get(wallet_id)
        if wallet is None or wallet.kind != Wallet.Kind.USER:
            raise ApiError(404, "wallet_not_found", "Wallet not found")
        allowed_from, to = TRANSITIONS[action]
        if wallet.status not in allowed_from:
            return reject(409, "invalid_state_transition", f"Cannot {action} a {wallet.status} wallet")
        if action == "close":
            if wallet.balance != 0:
                return reject(409, "balance_not_zero", "Wallet must have a zero balance to be closed", {"balance": m(wallet.balance)})
            pending = Withdrawal.objects.filter(
                wallet=wallet, status__in=[Withdrawal.Status.REQUESTED, Withdrawal.Status.PROCESSING]
            ).count()
            if pending:
                return reject(409, "pending_withdrawals", "Wallet has pending withdrawals", {"pending": pending})
        WalletStateHistory.objects.create(
            wallet=wallet, from_status=wallet.status, to_status=to, reason=reason, actor=actor.id
        )
        wallet.status = to
        wallet.status_reason = reason
        wallet.save(update_fields=["status", "status_reason"])
        return ok(200, wallet_dto(wallet))


def state_history(wallet_id) -> list[dict]:
    return [
        {"id": h.id, "from_status": h.from_status, "to_status": h.to_status, "reason": h.reason,
         "actor": h.actor, "created_at": h.created_at}
        for h in WalletStateHistory.objects.filter(wallet_id=wallet_id).order_by("id")
    ]


def set_daily_limit(wallet_id, limit: int | None) -> dict:
    with transaction.atomic():
        w = Wallet.objects.select_for_update().filter(id=wallet_id, kind=Wallet.Kind.USER).first()
        if w is None:
            raise ApiError(404, "wallet_not_found", "Wallet not found")
        w.daily_limit = limit
        w.save(update_fields=["daily_limit"])
    return wallet_dto(w)


def reverse_transfer(actor: Actor, transfer_id, reason: str) -> Result:
    """Reverse a transfer with a compensating journal (destination -> source). History is untouched.
    Fails (409) if the destination already spent the money, rather than creating a negative balance.
    Admin action: bypasses frozen status, daily limits and risk rules."""
    with transaction.atomic():
        t = Transaction.objects.filter(id=transfer_id).first()
        if t is None:
            raise ApiError(404, "transfer_not_found", "Transfer not found")
        if t.type != "transfer":
            return reject(422, "not_a_transfer", f"Only transfers can be reversed (this is a {t.type})")
        locked = lock_wallets([t.source_wallet_id, t.dest_wallet_id])
        src, dst = locked[t.source_wallet_id], locked[t.dest_wallet_id]

        existing = Transaction.objects.filter(reverses_id=t.id).first()
        if existing:
            return reject(409, "already_reversed", "Transfer was already reversed", {"reversal_id": str(existing.id)})
        if src.status == Wallet.Status.CLOSED:
            return reject(409, "source_closed", "Original sender wallet is closed")
        if dst.balance < t.amount:
            return reject(
                409, "insufficient_funds_to_reverse", "Destination no longer holds enough to reverse",
                {"balance": m(dst.balance), "required": m(t.amount)},
            )
        j = post_journal(
            type="reversal", amount=t.amount, source=dst, dest=src, reverses_id=t.id, reason=reason, created_by=actor.id
        )
        return ok(
            201,
            {"reversal_id": j.id, "reverses": t.id, "amount": m(t.amount), "from_wallet_id": dst.id,
             "to_wallet_id": src.id, "reason": reason, "created_at": j.created_at},
        )


def list_decisions(decision: str, limit: int, cursor: str | None) -> dict:
    wanted = ["flag", "block"] if decision == "all" else [decision]
    qs = RiskDecision.objects.filter(decision__in=wanted)
    if cursor is not None:
        qs = qs.filter(id__lt=int(cursor))
    rows = list(qs.order_by("-id")[: limit + 1])
    page = rows[:limit]
    return {
        "items": [
            {"id": r.id, "subject_type": r.subject_type, "subject_id": r.subject_id, "wallet_id": r.wallet_id,
             "amount": m(r.amount), "currency": r.currency, "decision": r.decision, "rule_id": r.rule_id,
             "rule_type": r.rule_type, "reason": r.reason, "details": r.details, "created_at": r.created_at}
            for r in page
        ],
        "next_cursor": str(page[-1].id) if len(rows) > limit else None,
    }
