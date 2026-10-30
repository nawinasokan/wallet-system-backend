import uuid

from tests.helpers import (ADMIN, balance, call, funded_wallet, key, parallel, reconcile_ok, sql, statuses,
                           transfer, u, user, wallet)


def test_100_simultaneous_transfers_out_of_one_wallet_never_overdraw_it():
    a, b = u("c-src"), u("c-dst")
    src, dst = funded_wallet(a, 5000), wallet(b)  # enough for exactly 50 x 100
    rs = parallel([lambda: transfer(user(a), src, dst, 100) for _ in range(100)])
    assert statuses(rs) == {201: 50, 422: 50}
    assert balance(src) == 0 and balance(dst) == 5000
    assert reconcile_ok()


def test_a_to_b_and_b_to_a_in_parallel_do_not_deadlock_and_conserve_money():
    a, b = u("d-a"), u("d-b")
    wa, wb = funded_wallet(a, 100_000), funded_wallet(b, 100_000)
    jobs = [
        (lambda: transfer(user(a), wa, wb, 7)) if i % 2 == 0 else (lambda: transfer(user(b), wb, wa, 3))
        for i in range(200)
    ]
    assert all(r.status == 201 for r in parallel(jobs))
    assert balance(wa) == 100_000 - 100 * 7 + 100 * 3
    assert balance(wb) == 100_000 + 100 * 7 - 100 * 3
    assert reconcile_ok()


def test_a_ring_of_transfers_across_many_wallets_completes_without_lock_cycles():
    owners = [u(f"ring-{i}") for i in range(6)]
    ids = [funded_wallet(o, 10_000) for o in owners]

    def job(i):
        frm = i % 6
        return lambda: transfer(user(owners[frm]), ids[frm], ids[(frm + 1 + (i % 4)) % 6], 5)

    assert all(r.status == 201 for r in parallel([job(i) for i in range(120)]))
    assert reconcile_ok()


def test_the_same_idempotency_key_sent_25_times_in_parallel_moves_money_once():
    a, b = u("i-a"), u("i-b")
    wa, wb = funded_wallet(a, 1000), wallet(b)
    k = str(uuid.uuid4())
    rs = parallel([lambda: transfer(user(a), wa, wb, 100, k) for _ in range(25)])
    assert len({r.body["transfer_id"] for r in rs}) == 1
    assert all(r.status == 201 for r in rs)
    assert balance(wa) == 900
    assert sql("SELECT count(*) FROM transactions WHERE type = 'transfer' AND source_wallet_id = %s", [wa])[0][0] == 1


def test_a_freeze_racing_in_flight_transfers_nothing_leaves_after_the_freeze_commits():
    a, b = u("f-a"), u("f-b")
    wa, wb = funded_wallet(a, 1_000_000), wallet(b)
    jobs = [lambda: transfer(user(a), wa, wb, 10) for _ in range(60)]
    jobs.append(lambda: call(ADMIN, "POST", f"/admin/wallets/{wa}/freeze", {"reason": "race test"}))
    results = parallel(jobs)
    transfers, freeze = results[:60], results[60]
    assert freeze.status == 200
    ok_n = sum(r.status == 201 for r in transfers)
    blocked = sum(r.status == 409 and r.body["error"]["code"] == "wallet_frozen" for r in transfers)
    assert ok_n + blocked == 60
    freeze_at = sql("SELECT created_at FROM wallet_state_history WHERE wallet_id = %s", [wa])[0][0]
    late = sql("SELECT count(*) FROM transactions WHERE source_wallet_id = %s AND type = 'transfer' AND created_at > %s",
               [wa, freeze_at])[0][0]
    assert late == 0
    assert balance(wa) == 1_000_000 - ok_n * 10
    assert transfer(user(a), wa, wb, 1).status == 409


def test_a_close_racing_a_transfer_never_strands_money_in_a_closed_wallet():
    s, t = u("cl-src"), u("cl-target")
    src, target = funded_wallet(s, 10_000), wallet(t)
    jobs = [lambda: transfer(user(s), src, target, 10) for _ in range(30)]
    jobs.append(lambda: call(ADMIN, "POST", f"/admin/wallets/{target}/close", {"reason": "race close"}))
    results = parallel(jobs)
    transfers, close = results[:30], results[30]
    bal = balance(target)
    assert bal == sum(r.status == 201 for r in transfers) * 10
    if close.status == 200:
        assert bal == 0  # closed only if nothing had arrived
    else:
        assert close.body["error"]["code"] == "balance_not_zero"
    status = sql("SELECT status FROM wallets WHERE id = %s", [target])[0][0]
    if status == "closed":
        assert bal == 0
    assert reconcile_ok()


def test_parallel_withdrawals_cannot_reserve_more_than_the_balance():
    o = u("pw-a")
    w = funded_wallet(o, 1000)
    dest = {"account_number": "123456785555", "account_holder": "X", "bank_code": "BANK"}
    rs = parallel([
        lambda: call(user(o), "POST", f"/wallets/{w}/withdrawals", {"amount": 100, "destination": dest}, key())
        for _ in range(30)
    ])
    assert statuses(rs) == {202: 10, 422: 20}
    assert balance(w) == 0
    assert reconcile_ok()
