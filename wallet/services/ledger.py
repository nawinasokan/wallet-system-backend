import uuid
from dataclasses import dataclass
from datetime import datetime

from django.db import connection

from ..errors import ApiError
from ..models import LedgerEntry, Transaction, Wallet


def lock_wallets(ids) -> dict[uuid.UUID, Wallet]:
    """Lock wallets in ascending id order. EVERY code path that locks more than one wallet goes
    through here, so two transactions can never wait on each other in a cycle (A->B vs B->A).
    The locks also serialise balance checks, state checks and risk-rule counting per wallet.
    Must run inside transaction.atomic()."""
    unique = sorted({i if isinstance(i, uuid.UUID) else uuid.UUID(str(i)) for i in ids})
    rows = Wallet.objects.select_for_update().filter(id__in=unique).order_by("id")
    return {w.id: w for w in rows}


def system_wallet(name: str, currency: str) -> Wallet:
    """Counter-party accounts (the outside world, funds held for in-flight withdrawals). Never locked."""
    owner = f"system:{name}"
    found = Wallet.objects.filter(owner_id=owner, currency=currency).first()
    if found is None:
        Wallet.objects.bulk_create(
            [Wallet(owner_id=owner, currency=currency, kind=Wallet.Kind.SYSTEM)], ignore_conflicts=True
        )
        found = Wallet.objects.get(owner_id=owner, currency=currency)
    return found


@dataclass
class Journal:
    id: uuid.UUID
    created_at: datetime


def post_journal(
    *,
    type: str,
    amount: int,
    source: Wallet,
    dest: Wallet,
    id: uuid.UUID | None = None,
    reverses_id: uuid.UUID | None = None,
    reason: str | None = None,
    created_by: str | None = None,
    metadata: dict | None = None,
) -> Journal:
    """Append one balanced journal: a debit on `source`, a credit on `dest`.
    The caller must already hold the row locks of any user wallets involved."""
    if dest.currency != source.currency:
        raise ApiError(500, "currency_mismatch", "Journal across currencies")
    txn = Transaction.objects.create(
        id=id or uuid.uuid4(),
        type=type,
        amount=amount,
        currency=source.currency,
        source_wallet=source,
        dest_wallet=dest,
        reverses_id=reverses_id,
        reason=reason,
        created_by=created_by,
        metadata=metadata or {},
    )
    for wallet, direction in ((source, "debit"), (dest, "credit")):
        balance_after = None
        if wallet.kind == Wallet.Kind.USER:
            delta = -amount if direction == "debit" else amount
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE wallets SET balance = balance + %s WHERE id = %s RETURNING balance",
                    [delta, wallet.id],
                )
                balance_after = cur.fetchone()[0]
        LedgerEntry.objects.create(
            transaction=txn, wallet=wallet, direction=direction, amount=amount, balance_after=balance_after
        )
    return Journal(txn.id, txn.created_at)


def spent_today(wallet_id) -> int:
    """Outgoing amount (transfers + withdrawals) since UTC midnight, excluding ones later undone."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(t.amount), 0)::bigint
                 FROM transactions t
                WHERE t.source_wallet_id = %s
                  AND t.type IN ('transfer', 'withdrawal')
                  AND t.created_at >= date_trunc('day', clock_timestamp() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                  AND NOT EXISTS (SELECT 1 FROM transactions r WHERE r.reverses_id = t.id)""",
            [wallet_id],
        )
        return cur.fetchone()[0]
