import uuid

import pytest

from tests.helpers import ADMIN, balance, call, funded_wallet, parallel, sql, transfer, u, user, wallet
from wallet.risk.engine import RiskEngine, set_engine

RULES = [
    {"id": "vel", "type": "velocity_outgoing_transfers", "action": "block", "params": {"maxTransfers": 3, "windowMinutes": 10}},
    {"id": "newbie", "type": "new_wallet_large_amount", "action": "flag", "params": {"amountAbove": 5000, "walletAgeDays": 7}},
    {"id": "topup-out", "type": "withdrawal_after_large_topup", "action": "block", "params": {"topupAtLeast": 20000, "windowMinutes": 60}},
]
DEST = {"account_number": "123456785555", "account_holder": "X", "bank_code": "BANK"}


@pytest.fixture(autouse=True)
def _risk_engine():
    set_engine(RiskEngine.from_configs(RULES))
    yield
    set_engine(None)


def withdraw(owner, wid, amount):
    return call(user(owner), "POST", f"/wallets/{wid}/withdrawals", {"amount": amount, "destination": DEST},
                {"idempotency-key": str(uuid.uuid4())})


def age(wid):
    sql("UPDATE wallets SET created_at = now() - interval '30 days' WHERE id = %s", [wid])


def test_velocity_blocks_the_4th_transfer_and_records_the_triggering_rule():
    a, b = u("v-a"), u("v-b")
    wa, wb = funded_wallet(a, 1000), wallet(b)
    age(wa)
    for _ in range(3):
        assert transfer(user(a), wa, wb, 1).status == 201
    r = transfer(user(a), wa, wb, 1)
    assert r.status == 403 and r.body["error"]["code"] == "blocked_by_risk_rule"
    assert r.body["error"]["details"]["rules"][0]["id"] == "vel"
    assert balance(wa) == 997
    rows = sql("SELECT decision, rule_id FROM risk_decisions WHERE wallet_id = %s ORDER BY id", [wa])
    assert [f"{d}:{r}" for d, r in rows] == ["allow:None"] * 3 + ["block:vel"]


def test_velocity_holds_under_concurrency_10_parallel_requests_only_3_get_through():
    a, b = u("vc-a"), u("vc-b")
    wa, wb = funded_wallet(a, 1000), wallet(b)
    age(wa)
    rs = parallel([lambda: transfer(user(a), wa, wb, 1) for _ in range(10)])
    assert sum(r.status == 201 for r in rs) == 3
    assert sum(r.status == 403 for r in rs) == 7
    assert balance(wa) == 997


def test_new_wallet_large_amount_flags_a_transfer_and_a_withdrawal_admin_sees_them():
    a, b = u("n-a"), u("n-b")
    wa, wb = funded_wallet(a, 15_000), wallet(b)  # young wallet, top-up below the large-top-up threshold
    tr = transfer(user(a), wa, wb, 6000)
    assert tr.status == 201 and tr.body["risk"]["decision"] == "flag"
    assert transfer(user(a), wa, wb, 100).body["risk"]["decision"] == "allow"
    wd = withdraw(a, wa, 7000)
    assert wd.status == 202 and wd.body["risk"]["decision"] == "flag"

    flags = call(ADMIN, "GET", "/admin/flags?limit=200").body["items"]
    mine = [f for f in flags if f["wallet_id"] == wa]
    assert [[f["subject_type"], f["rule_id"], f["amount"]] for f in mine] == [
        ["withdrawal", "newbie", "7000"], ["transfer", "newbie", "6000"]]
    assert mine[1]["subject_id"] == tr.body["transfer_id"]
    assert mine[0]["subject_id"] == wd.body["id"]
    assert call(user(a), "GET", "/admin/flags").status == 403


def test_an_old_wallet_making_the_same_large_transfer_is_not_flagged():
    a, b = u("o-a"), u("o-b")
    wa, wb = funded_wallet(a, 50_000), wallet(b)
    age(wa)
    assert transfer(user(a), wa, wb, 6000).body["risk"]["decision"] == "allow"


def test_withdrawal_soon_after_a_large_topup_is_blocked_a_transfer_is_not():
    a, b = u("t-a"), u("t-b")
    wa, wb = funded_wallet(a, 25_000), wallet(b)  # top-up of 25_000 >= 20_000 just happened
    age(wa)
    r = withdraw(a, wa, 100)
    assert r.status == 403
    assert r.body["error"]["details"]["rules"][0]["id"] == "topup-out"
    assert balance(wa) == 25_000  # nothing reserved
    assert transfer(user(a), wa, wb, 100).status == 201
    blocked = call(ADMIN, "GET", "/admin/flags?decision=block&limit=200").body["items"]
    assert any(f["wallet_id"] == wa and f["rule_id"] == "topup-out" and f["subject_type"] == "withdrawal" for f in blocked)


def test_withdrawals_pass_once_the_topup_is_outside_the_window():
    a = u("t-c")
    wa = funded_wallet(a, 25_000)
    age(wa)
    sql("ALTER TABLE transactions DISABLE TRIGGER transactions_immutable")
    try:
        sql("UPDATE transactions SET created_at = now() - interval '2 hours' WHERE dest_wallet_id = %s", [wa])
    finally:
        sql("ALTER TABLE transactions ENABLE TRIGGER transactions_immutable")
    assert withdraw(a, wa, 100).status == 202


def test_blocked_attempts_leave_the_balance_and_ledger_untouched():
    a, b = u("z-a"), u("z-b")
    wa, wb = funded_wallet(a, 100), wallet(b)
    age(wa)
    for _ in range(3):
        transfer(user(a), wa, wb, 1)
    before = sql("SELECT count(*) FROM ledger_entries")[0][0]
    transfer(user(a), wa, wb, 1)
    assert sql("SELECT count(*) FROM ledger_entries")[0][0] == before
