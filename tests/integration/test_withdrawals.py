import time
import uuid

import pytest
from django.db import IntegrityError

from tests.helpers import (ADMIN, balance, bank_callback, call, funded_wallet, key, parallel, reconcile_ok, sql, u, user)
from wallet.services.reconciliation import reconcile
from wallet.services.withdrawals import sweep_timeouts


def dest(suffix="5555"):
    return {"account_number": f"12345678{suffix}", "account_holder": "Jane", "bank_code": "BANK"}


def request(owner, wid, amount, suffix="5555", idem=None):
    return call(user(owner), "POST", f"/wallets/{wid}/withdrawals", {"amount": amount, "destination": dest(suffix)},
                {"idempotency-key": idem or str(uuid.uuid4())})


def status(wid):
    return call(ADMIN, "GET", f"/withdrawals/{wid}").body["status"]


def cb(w, **over):
    return {"event_id": f"evt_{uuid.uuid4()}", "withdrawal_id": w["id"], "bank_reference": "bank_ref",
            "status": "completed", **over}


def ledger_rows(type_, wid):
    return sql("SELECT count(*) FROM transactions WHERE type = %s AND metadata->>'withdrawal_id' = %s", [type_, wid])[0][0]


class TestHappyPath:
    def test_reserves_funds_on_request_then_settles_on_bank_success(self, bank):
        o = u("w-ok")
        w = funded_wallet(o, 1000)
        r = request(o, w, 600)
        assert r.status == 202 and r.body["status"] == "requested"
        assert balance(w) == 400  # reserved immediately
        assert request(o, w, 500).status == 422  # cannot be spent twice

        bank.run_worker()
        assert status(r.body["id"]) == "processing"
        assert bank.outbox[-1]["withdrawal_id"] == r.body["id"]
        assert bank_callback(bank.outbox[-1]).body["result"] == "applied"
        assert status(r.body["id"]) == "completed"
        assert balance(w) == 400
        assert reconcile_ok()

    def test_only_the_owner_or_an_admin_can_request_or_view(self):
        o = u("w-own")
        w = funded_wallet(o, 1000)
        assert request(u("someone-else"), w, 10).status == 403
        r = request(o, w, 10)
        assert call(user(u("someone-else")), "GET", f"/withdrawals/{r.body['id']}").status == 403


class TestFailureAndRefund:
    def test_a_bank_rejection_refunds_through_compensating_entries_exactly_once(self, bank):
        o = u("w-fail")
        w = funded_wallet(o, 1000)
        r = request(o, w, 700, "0000")
        assert balance(w) == 300
        bank.run_worker()
        failure = next(p for p in bank.outbox if p["withdrawal_id"] == r.body["id"])
        assert failure["status"] == "failed"

        assert bank_callback(failure).body["result"] == "applied"
        assert status(r.body["id"]) == "failed"
        assert balance(w) == 1000
        assert ledger_rows("withdrawal_refund", r.body["id"]) == 1

        # duplicate delivery (same event id) and a re-sent failure (new event id): no second refund
        assert bank_callback(failure).body["result"] == "duplicate_event"
        assert bank_callback(cb(r.body, status="failed")).body["result"] == "ignored_already_final"
        assert balance(w) == 1000
        assert ledger_rows("withdrawal_refund", r.body["id"]) == 1

        st = call(user(o), "GET", f"/wallets/{w}/transactions").body["items"]
        assert [f"{i['type']}:{i['direction']}" for i in st] == [
            "withdrawal_refund:credit", "withdrawal:debit", "topup:credit"]
        rec = reconcile()
        assert rec["ok"] and rec["ledger_imbalance"] == "0"

    def test_the_refund_can_happen_even_though_the_wallet_was_frozen_meanwhile(self, bank):
        o = u("w-fz")
        w = funded_wallet(o, 1000)
        r = request(o, w, 400)
        bank.run_worker()
        call(ADMIN, "POST", f"/admin/wallets/{w}/freeze", {"reason": "investigation"})
        bank_callback(cb(r.body, status="failed", failure_reason="rejected"))
        assert balance(w) == 1000

    def test_a_synchronous_bank_rejection_at_submit_refunds_immediately(self, bank):
        o = u("w-sync")
        w = funded_wallet(o, 1000)
        r = request(o, w, 250, "1111")
        bank.run_worker()
        assert status(r.body["id"]) == "failed"
        assert balance(w) == 1000

    def test_concurrent_duplicate_and_conflicting_callbacks_apply_exactly_one_outcome(self, bank):
        o = u("w-race")
        w = funded_wallet(o, 1000)
        r = request(o, w, 500)
        bank.run_worker()
        events = [cb(r.body, status="failed"), cb(r.body, status="failed"), cb(r.body, status="completed"),
                  cb(r.body, status="completed"), cb(r.body, status="failed")]
        rs = parallel([(lambda e=e: bank_callback(e)) for e in [*events, events[0], events[2]]])
        assert all(x.status == 200 for x in rs)
        assert sum(x.body["result"] == "applied" for x in rs) == 1
        final = status(r.body["id"])
        assert ledger_rows("withdrawal_refund", r.body["id"]) + ledger_rows("withdrawal_settle", r.body["id"]) == 1
        assert balance(w) == (1000 if final == "failed" else 500)
        assert reconcile_ok()

    def test_the_database_itself_refuses_a_second_refund_for_the_same_withdrawal(self, bank):
        o = u("w-db")
        w = funded_wallet(o, 1000)
        r = request(o, w, 400)
        bank.run_worker()
        bank_callback(cb(r.body, status="failed"))
        with pytest.raises(IntegrityError):
            sql("""INSERT INTO transactions (id, type, amount, currency, source_wallet_id, dest_wallet_id, reverses_id, metadata, created_at)
                   SELECT gen_random_uuid(), 'withdrawal_refund', amount, currency, source_wallet_id, dest_wallet_id,
                          reverses_id, '{}', now()
                     FROM transactions WHERE type = 'withdrawal_refund' AND metadata->>'withdrawal_id' = %s""", [r.body["id"]])


class TestTimeoutsAndWebhooks:
    def test_a_silent_bank_times_out_fails_and_refunds_once_a_late_success_is_a_conflict(self, bank):
        o = u("w-to")
        w = funded_wallet(o, 1000)
        r = request(o, w, 300, "9999")
        bank.run_worker()
        assert [p for p in bank.outbox if p["withdrawal_id"] == r.body["id"]] == []  # silence
        assert sweep_timeouts(60_000) == 0  # not yet
        assert status(r.body["id"]) == "processing"

        sql("UPDATE withdrawals SET processing_started_at = now() - interval '5 minutes' WHERE id = %s", [r.body["id"]])
        assert sweep_timeouts(60_000) == 1
        assert sweep_timeouts(60_000) == 0  # second sweep does nothing
        assert status(r.body["id"]) == "failed"
        assert call(ADMIN, "GET", f"/withdrawals/{r.body['id']}").body["failure_reason"] == "bank_timeout"
        assert balance(w) == 1000

        late = bank_callback(cb(r.body, status="completed"))
        assert late.body["result"] == "conflict_recorded"
        assert balance(w) == 1000
        assert sql("SELECT outcome FROM bank_callbacks WHERE withdrawal_id = %s", [r.body["id"]])[0][0] == "conflict"

    def test_mock_bank_7777_duplicate_and_redelivered_success_callbacks_settle_once(self, bank):
        o = u("w-dup")
        w = funded_wallet(o, 1000)
        r = request(o, w, 300, "7777")
        bank.run_worker()
        mine = [p for p in bank.outbox if p["withdrawal_id"] == r.body["id"]]
        assert len(mine) == 3
        for p in mine:
            assert bank_callback(p).status == 200
        assert status(r.body["id"]) == "completed"
        assert ledger_rows("withdrawal_settle", r.body["id"]) == 1
        assert balance(w) == 700

    def test_rejects_badly_signed_callbacks_and_callbacks_for_unsubmitted_withdrawals(self, bank):
        from rest_framework.test import APIClient
        import json

        o = u("w-sig")
        w = funded_wallet(o, 1000)
        r = request(o, w, 100)
        payload = cb(r.body)
        bad = APIClient().generic("POST", "/bank/callbacks", data=json.dumps(payload),
                                  content_type="application/json", HTTP_X_BANK_SIGNATURE="deadbeef")
        assert bad.status_code == 401
        none = APIClient().generic("POST", "/bank/callbacks", data=json.dumps(payload), content_type="application/json")
        assert none.status_code == 401
        early = bank_callback(payload)  # still `requested`
        assert early.status == 409
        assert status(r.body["id"]) == "requested"
        bank.run_worker()
        assert bank_callback(payload).body["result"] == "applied"  # the 409 did not burn the event id

    def test_unknown_withdrawal_is_a_404(self):
        r = bank_callback({"event_id": f"evt_{uuid.uuid4()}", "withdrawal_id": str(uuid.uuid4()),
                           "bank_reference": "x", "status": "completed"})
        assert r.status == 404


class TestStateLimitsAndIdempotency:
    def test_a_frozen_wallet_cannot_withdraw(self):
        o = u("wf-a")
        w = funded_wallet(o, 1000)
        call(ADMIN, "POST", f"/admin/wallets/{w}/freeze", {"reason": "fraud review"})
        r = request(o, w, 100)
        assert r.status == 409 and r.body["error"]["code"] == "wallet_frozen"
        assert balance(w) == 1000

    def test_cannot_close_a_wallet_with_a_pending_withdrawal_can_once_resolved(self, bank):
        o = u("wf-b")
        w = funded_wallet(o, 500)
        r = request(o, w, 500)  # balance now 0 but funds are in flight
        no = call(ADMIN, "POST", f"/admin/wallets/{w}/close", {"reason": "closing up"})
        assert no.status == 409 and no.body["error"]["code"] == "pending_withdrawals"
        bank.run_worker()
        bank_callback(cb(r.body))
        assert call(ADMIN, "POST", f"/admin/wallets/{w}/close", {"reason": "closing up"}).status == 200

    def test_daily_limit_counts_withdrawals_a_failed_withdrawal_frees_its_share(self, bank):
        o = u("wf-c")
        w = funded_wallet(o, 5000)
        call(ADMIN, "PUT", f"/admin/wallets/{w}/daily-limit", {"daily_limit": 1000})
        a = request(o, w, 800)
        assert a.status == 202
        over = request(o, w, 300)
        assert over.status == 422 and over.body["error"]["code"] == "daily_limit_exceeded"
        bank.run_worker()
        bank_callback(cb(a.body, status="failed"))
        assert request(o, w, 900).status == 202

    def test_retrying_with_the_same_idempotency_key_reserves_funds_once(self):
        o = u("wf-d")
        w = funded_wallet(o, 1000)
        k = str(uuid.uuid4())
        rs = parallel([lambda: request(o, w, 400, "5555", k) for _ in range(8)])
        assert len({x.body["id"] for x in rs}) == 1
        assert balance(w) == 600
        assert sql("SELECT count(*) FROM withdrawals WHERE wallet_id = %s", [w])[0][0] == 1

    def test_end_to_end_through_the_worker_and_mock_bank(self, bank):
        o = u("wf-e")
        w = funded_wallet(o, 1000)
        good, bad = request(o, w, 100, "5555"), request(o, w, 200, "0000")
        bank.run_worker()
        for p in [p for p in bank.outbox if p["withdrawal_id"] in (good.body["id"], bad.body["id"])]:
            bank_callback(p)
        assert status(good.body["id"]) == "completed"
        assert status(bad.body["id"]) == "failed"
        assert balance(w) == 900

    def test_invalid_destination_is_a_400(self):
        o = u("wf-f")
        w = funded_wallet(o, 1000)
        r = call(user(o), "POST", f"/wallets/{w}/withdrawals",
                 {"amount": 10, "destination": {"account_number": "12", "account_holder": "x", "bank_code": "B"}}, key())
        assert r.status == 400
