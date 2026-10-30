import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction

from ..auth import Actor, assert_can_access
from ..bank import MockBank
from ..dbfuncs import ClockTimestamp
from ..errors import ApiError, Result, ok, reject
from ..models import BankCallback, Wallet, Withdrawal
from ..money import m
from ..risk.engine import get_engine
from ..risk.types import RuleContext
from .ledger import lock_wallets, post_journal, spent_today, system_wallet


def withdrawal_dto(w: Withdrawal) -> dict:
    return {
        "id": w.id,
        "wallet_id": w.wallet_id,
        "amount": m(w.amount),
        "currency": w.currency,
        "destination": w.destination,
        "status": w.status,
        "failure_reason": w.failure_reason,
        "bank_reference": w.bank_reference,
        "created_at": w.created_at,
        "updated_at": w.updated_at,
    }


def request_withdrawal(actor: Actor, wallet_id, amount: int, destination: dict) -> Result:
    """Funds move wallet -> withdrawal_hold in the same transaction that creates the request, so
    they are reserved atomically and cannot be spent twice."""
    wallet = lock_wallets([wallet_id]).get(wallet_id)
    if wallet is None:
        raise ApiError(404, "wallet_not_found", "Wallet not found")
    assert_can_access(actor, wallet)

    if wallet.status == Wallet.Status.FROZEN:
        return reject(409, "wallet_frozen", "Wallet is frozen; withdrawals are blocked")
    if wallet.status == Wallet.Status.CLOSED:
        return reject(409, "wallet_closed", "Wallet is closed")
    if wallet.balance < amount:
        return reject(422, "insufficient_funds", "Insufficient funds", {"balance": m(wallet.balance)})

    limit = wallet.daily_limit if wallet.daily_limit is not None else settings.DEFAULT_DAILY_LIMIT
    spent = spent_today(wallet.id)
    if spent + amount > limit:
        return reject(422, "daily_limit_exceeded", "Daily limit exceeded", {"limit": m(limit), "spent_today": m(spent)})

    withdrawal_id = uuid.uuid4()
    verdict = get_engine().evaluate(RuleContext(wallet, "withdrawal", amount), withdrawal_id)
    if verdict.decision == "block":
        return reject(403, "blocked_by_risk_rule", "Withdrawal blocked by risk rule", {"rules": verdict.rules_payload()})

    hold = system_wallet("withdrawal_hold", wallet.currency)
    j = post_journal(
        type="withdrawal",
        amount=amount,
        source=wallet,
        dest=hold,
        created_by=actor.id,
        metadata={"withdrawal_id": str(withdrawal_id)},
    )
    w = Withdrawal.objects.create(
        id=withdrawal_id,
        wallet=wallet,
        amount=amount,
        currency=wallet.currency,
        destination=destination,
        status=Withdrawal.Status.REQUESTED,
        hold_txn_id=j.id,
        created_by=actor.id,
    )
    return ok(202, {**withdrawal_dto(w), "risk": {"decision": verdict.decision, "rules": verdict.rules_payload()}})


def get_withdrawal(actor: Actor, withdrawal_id) -> dict:
    w = Withdrawal.objects.select_related("wallet").filter(id=withdrawal_id).first()
    if w is None:
        raise ApiError(404, "withdrawal_not_found", "Withdrawal not found")
    assert_can_access(actor, w.wallet)
    return withdrawal_dto(w)


def _complete_locked(w: Withdrawal, bank_reference: str | None) -> None:
    """Terminal success: funds leave the hold account for the outside world."""
    hold = system_wallet("withdrawal_hold", w.currency)
    external = system_wallet("external", w.currency)
    j = post_journal(
        type="withdrawal_settle", amount=w.amount, source=hold, dest=external, metadata={"withdrawal_id": str(w.id)}
    )
    w.status = Withdrawal.Status.COMPLETED
    w.resolution_txn_id = j.id
    if bank_reference:
        w.bank_reference = bank_reference
    w.updated_at = ClockTimestamp()
    w.save(update_fields=["status", "resolution_txn", "bank_reference", "updated_at"])


def _fail_locked(w: Withdrawal, reason: str) -> None:
    """Terminal failure: a compensating journal returns the reserved funds. `reverses_id` is UNIQUE,
    so even a bug in the state machine could not refund the same withdrawal twice. Refunds ignore
    wallet status: a frozen wallet can still receive its own money back."""
    wallet = lock_wallets([w.wallet_id])[w.wallet_id]
    hold = system_wallet("withdrawal_hold", w.currency)
    j = post_journal(
        type="withdrawal_refund",
        amount=w.amount,
        source=hold,
        dest=wallet,
        reverses_id=w.hold_txn_id,
        reason=reason,
        metadata={"withdrawal_id": str(w.id)},
    )
    w.status = Withdrawal.Status.FAILED
    w.failure_reason = reason
    w.resolution_txn_id = j.id
    w.updated_at = ClockTimestamp()
    w.save(update_fields=["status", "failure_reason", "resolution_txn", "updated_at"])


def _lock_withdrawal(withdrawal_id) -> Withdrawal | None:
    return Withdrawal.objects.select_for_update().filter(id=withdrawal_id).first()


def apply_bank_callback(payload: dict) -> Result:
    """Apply a bank callback. Safe against duplicates, redelivery and out-of-order delivery:
      * event_id is recorded (PK), so an identical redelivery is a no-op;
      * the withdrawal row is locked and only `processing` can transition, so a second event with
        the same outcome is ignored and a *contradicting* one is recorded as a conflict."""
    with transaction.atomic():
        created = BankCallback.objects.bulk_create(
            [BankCallback(event_id=payload["event_id"], withdrawal_id=payload["withdrawal_id"], payload=payload, outcome="pending")],
            ignore_conflicts=True,
        )
        fresh = BankCallback.objects.filter(event_id=payload["event_id"], outcome="pending").exists()
        if not created or not fresh:
            return ok(200, {"result": "duplicate_event"})

        def set_outcome(o: str):
            BankCallback.objects.filter(event_id=payload["event_id"]).update(outcome=o)

        w = _lock_withdrawal(payload["withdrawal_id"])
        if w is None:
            set_outcome("unknown_withdrawal")
            return reject(404, "withdrawal_not_found", "Unknown withdrawal")
        if w.status == Withdrawal.Status.REQUESTED:
            # The bank cannot know about a payment we have not submitted. Do not consume the event.
            raise ApiError(409, "not_submitted", "Withdrawal has not been submitted to the bank")
        if w.status == Withdrawal.Status.PROCESSING:
            if payload["status"] == "completed":
                _complete_locked(w, payload.get("bank_reference"))
            else:
                _fail_locked(w, payload.get("failure_reason") or "bank_rejected")
            set_outcome("applied")
            return ok(200, {"result": "applied", "status": payload["status"]})
        agrees = (w.status == Withdrawal.Status.COMPLETED) == (payload["status"] == "completed")
        set_outcome("ignored_terminal" if agrees else "conflict")
        return ok(200, {"result": "ignored_already_final" if agrees else "conflict_recorded", "status": w.status})


def submit_pending(bank: MockBank, batch: int = 20) -> int:
    """Worker step 1: claim `requested` withdrawals (SKIP LOCKED) and hand them to the bank."""
    with transaction.atomic():
        ids = list(
            Withdrawal.objects.select_for_update(skip_locked=True)
            .filter(status=Withdrawal.Status.REQUESTED)
            .order_by("created_at")
            .values_list("id", flat=True)[:batch]
        )
        Withdrawal.objects.filter(id__in=ids).update(
            status=Withdrawal.Status.PROCESSING, processing_started_at=ClockTimestamp(), updated_at=ClockTimestamp()
        )
        claimed = list(Withdrawal.objects.filter(id__in=ids))
    for w in claimed:
        try:
            res = bank.submit(w.id, w.destination)
        except Exception:
            continue  # bank unreachable: stays `processing`; the timeout sweep resolves it
        if res.accepted:
            Withdrawal.objects.filter(id=w.id, status=Withdrawal.Status.PROCESSING).update(bank_reference=res.reference)
        else:
            with transaction.atomic():
                cur = _lock_withdrawal(w.id)
                if cur and cur.status == Withdrawal.Status.PROCESSING:
                    _fail_locked(cur, f"bank_rejected:{res.reason}")
    return len(claimed)


def sweep_timeouts(older_than_ms: int | None = None) -> int:
    """Worker step 2: withdrawals the bank never answered are failed and refunded (once)."""
    ms = settings.BANK_TIMEOUT_MS if older_than_ms is None else older_than_ms
    ids = list(
        Withdrawal.objects.filter(
            status=Withdrawal.Status.PROCESSING,
            processing_started_at__lt=ClockTimestamp() - timedelta(milliseconds=ms),
        ).values_list("id", flat=True)
    )
    n = 0
    for wid in ids:
        with transaction.atomic():
            w = _lock_withdrawal(wid)
            if w and w.status == Withdrawal.Status.PROCESSING:
                _fail_locked(w, "bank_timeout")
                n += 1
    return n
