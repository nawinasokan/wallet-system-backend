from django.db import connection

from ..auth import Actor, assert_can_access
from ..errors import ApiError, jsonable
from ..money import m
from .wallets import get_wallet_or_404


def statement(actor: Actor, wallet_id, *, date_from, date_to, limit: int, cursor: str | None) -> dict:
    """Ledger entries for a wallet, newest first, keyset-paginated on the entry id."""
    wallet = get_wallet_or_404(wallet_id)
    assert_can_access(actor, wallet)
    cursor_id = None
    if cursor:
        if not cursor.isdigit() or len(cursor) > 18:
            raise ApiError(400, "invalid_cursor", "Invalid cursor")
        cursor_id = int(cursor)
    with connection.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.transaction_id, t.type, e.direction, e.amount, e.balance_after, t.currency,
                      CASE WHEN e.direction = 'debit' THEN t.dest_wallet_id ELSE t.source_wallet_id END,
                      t.reverses_id, t.reason, e.created_at
                 FROM ledger_entries e JOIN transactions t ON t.id = e.transaction_id
                WHERE e.wallet_id = %s
                  AND (%s::timestamptz IS NULL OR e.created_at >= %s)
                  AND (%s::timestamptz IS NULL OR e.created_at <  %s)
                  AND (%s::bigint IS NULL OR e.id < %s)
                ORDER BY e.id DESC
                LIMIT %s""",
            [wallet.id, date_from, date_from, date_to, date_to, cursor_id, cursor_id, limit + 1],
        )
        rows = cur.fetchall()
    items = [
        {"entry_id": r[0], "transaction_id": r[1], "type": r[2], "direction": r[3], "amount": m(r[4]),
         "balance_after": m(r[5]), "currency": r[6], "counterparty_wallet_id": r[7], "reverses_id": r[8],
         "reason": r[9], "created_at": r[10]}
        for r in rows[:limit]
    ]
    return jsonable({"items": items, "next_cursor": str(items[-1]["entry_id"]) if len(rows) > limit else None})
