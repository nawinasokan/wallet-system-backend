from django.db import connection

from ..money import m


def reconcile() -> dict:
    """Verify stored balances against the ledger, that debits equal credits, and that the hold
    account equals the sum of pending withdrawals."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT w.id, w.balance,
                      COALESCE(SUM(CASE e.direction WHEN 'credit' THEN e.amount ELSE -e.amount END), 0)::bigint
                 FROM wallets w LEFT JOIN ledger_entries e ON e.wallet_id = w.id
                WHERE w.kind = 'user'
                GROUP BY w.id, w.balance
               HAVING w.balance <> COALESCE(SUM(CASE e.direction WHEN 'credit' THEN e.amount ELSE -e.amount END), 0)"""
        )
        wallets = [
            {"wallet_id": str(r[0]), "stored_balance": m(r[1]), "ledger_balance": m(r[2])} for r in cur.fetchall()
        ]
        cur.execute(
            "SELECT COALESCE(SUM(CASE direction WHEN 'debit' THEN amount ELSE -amount END), 0)::bigint FROM ledger_entries"
        )
        imbalance = cur.fetchone()[0]
        cur.execute(
            """SELECT h.currency, h.hold_balance, COALESCE(p.pending, 0)::bigint
                 FROM (SELECT w.currency,
                              COALESCE(SUM(CASE e.direction WHEN 'credit' THEN e.amount ELSE -e.amount END), 0)::bigint AS hold_balance
                         FROM wallets w LEFT JOIN ledger_entries e ON e.wallet_id = w.id
                        WHERE w.owner_id = 'system:withdrawal_hold' GROUP BY w.currency) h
                 LEFT JOIN (SELECT currency, SUM(amount) AS pending FROM withdrawals
                             WHERE status IN ('requested', 'processing') GROUP BY currency) p
                        ON p.currency = h.currency
                WHERE h.hold_balance <> COALESCE(p.pending, 0)"""
        )
        holds = [
            {"currency": r[0], "hold_balance": m(r[1]), "pending_withdrawals": m(r[2])} for r in cur.fetchall()
        ]
    return {
        "ok": not wallets and imbalance == 0 and not holds,
        "wallet_mismatches": wallets,
        "ledger_imbalance": m(imbalance),
        "hold_mismatches": holds,
    }
