import uuid

import pytest
from django.db import DatabaseError, connection, transaction

from tests.helpers import ADMIN, Bank, balance, bank_callback, call, funded_wallet, key, parallel, sql, transfer, u, user
from wallet.services.reconciliation import reconcile


def test_after_a_mixed_workload_every_balance_equals_credits_minus_debits_and_ledger_sums_to_zero(bank):
    owners = [u(f"r-{i}") for i in range(5)]
    ids = [funded_wallet(o, 50_000) for o in owners]
    dest = {"account_number": "123456785555", "account_holder": "X", "bank_code": "B"}
    jobs = []
    for i in range(150):
        frm, to = i % 5, (i * 3 + 1) % 5
        if frm == to:
            continue
        jobs.append(lambda frm=frm, to=to, i=i: transfer(user(owners[frm]), ids[frm], ids[to], 1 + (i % 50)))
        if i % 10 == 0:
            jobs.append(lambda frm=frm: call(user(owners[frm]), "POST", f"/wallets/{ids[frm]}/withdrawals",
                                             {"amount": 100, "destination": dest}, key()))
        if i % 25 == 0:
            jobs.append(lambda to=to: call(user(owners[to]), "POST", f"/wallets/{ids[to]}/topups",
                                           {"amount": 777, "payment_method": "tok"}, key()))
    parallel(jobs)
    # reverse a few transfers and fail every withdrawal
    mine = tuple(ids)
    some = sql("SELECT id FROM transactions WHERE type = 'transfer' AND source_wallet_id = ANY(%s::uuid[]) "
               "ORDER BY created_at LIMIT 5", [list(mine)])
    for (tid,) in some:
        call(ADMIN, "POST", f"/admin/transfers/{tid}/reverse", {"reason": "recon test"})
    bank.run_worker()
    for p in bank.outbox:
        bank_callback({**p, "status": "failed"})

    assert reconcile() == {"ok": True, "wallet_mismatches": [], "ledger_imbalance": "0", "hold_mismatches": []}

    # independent re-derivation, straight from the ledger, per wallet
    rows = sql("""SELECT w.balance, COALESCE(SUM(CASE e.direction WHEN 'credit' THEN e.amount ELSE -e.amount END), 0)::bigint
                    FROM wallets w LEFT JOIN ledger_entries e ON e.wallet_id = w.id
                   WHERE w.kind = 'user' GROUP BY w.id, w.balance""")
    assert len(rows) >= 5
    assert all(b == ledger for b, ledger in rows)

    # and the last statement row's running balance equals the wallet balance
    for wid in ids:
        st = call(ADMIN, "GET", f"/wallets/{wid}/transactions?limit=1")
        assert int(st.body["items"][0]["balance_after"]) == balance(wid)
    assert call(ADMIN, "GET", "/admin/reconciliation").body["ok"] is True


def test_detects_tampering_a_drifted_balance_instead_of_hiding_it():
    w = funded_wallet(u("r-tamper"), 1000)
    sql("UPDATE wallets SET balance = balance + 1 WHERE id = %s", [w])
    try:
        report = reconcile()
        assert report["ok"] is False
        assert report["wallet_mismatches"] == [{"wallet_id": w, "stored_balance": "1001", "ledger_balance": "1000"}]
    finally:
        sql("UPDATE wallets SET balance = balance - 1 WHERE id = %s", [w])
    assert reconcile()["ok"] is True


def test_the_database_refuses_an_unbalanced_journal_and_a_negative_balance():
    w = funded_wallet(u("r-guard"), 1000)
    with pytest.raises(DatabaseError, match="balance_non_negative"):
        sql("UPDATE wallets SET balance = -1 WHERE id = %s", [w])
    sys_id = sql("SELECT id FROM wallets WHERE owner_id = 'system:external' LIMIT 1")[0][0]
    tid = uuid.uuid4()
    with pytest.raises(DatabaseError, match="unbalanced journal"):
        with transaction.atomic():
            sql("INSERT INTO transactions (id, type, amount, currency, source_wallet_id, dest_wallet_id, metadata, created_at)"
                " VALUES (%s, 'topup', 10, 'USD', %s, %s, '{}', now())", [tid, sys_id, w])
            sql("INSERT INTO ledger_entries (transaction_id, wallet_id, direction, amount, created_at)"
                " VALUES (%s, %s, 'credit', 10, now())", [tid, w])
    assert balance(w) == 1000
