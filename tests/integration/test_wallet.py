import json
import uuid
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import pytest
from rest_framework.test import APIClient

from tests.helpers import ADMIN, balance, call, funded_wallet, key, reconcile_ok, sql, transfer, u, user, wallet


class TestAuth:
    def test_rejects_missing_and_bad_credentials(self):
        assert call(None, "GET", f"/wallets/{uuid.uuid4()}").status == 401
        r = APIClient().get(f"/wallets/{uuid.uuid4()}", HTTP_AUTHORIZATION="Bearer garbage")
        assert r.status_code == 401

    def test_users_reach_only_their_own_wallets_admins_reach_all(self):
        a, b = u("auth-a"), u("auth-b")
        w = wallet(a)
        assert call(user(a), "GET", f"/wallets/{w}").status == 200
        assert call(user(b), "GET", f"/wallets/{w}").status == 403
        assert call(ADMIN, "GET", f"/wallets/{w}").status == 200
        assert call(user(b), "POST", f"/wallets/{w}/topups", {"amount": 5, "payment_method": "x"}, key()).status == 403
        assert call(user(b), "GET", f"/wallets/{w}/transactions").status == 403

    def test_non_admins_cannot_call_admin_endpoints(self):
        c = u("auth-c")
        w = wallet(c)
        assert call(user(c), "POST", f"/admin/wallets/{w}/freeze", {"reason": "because"}).status == 403
        assert call(user(c), "GET", "/admin/flags").status == 403

    def test_static_api_key(self, settings):
        settings.API_KEYS = {"k-user": {"sub": "key-user", "role": "user"}, "k-admin": {"sub": "ops", "role": "admin"}}
        client = APIClient()
        assert client.post("/wallets", {"currency": "USD"}, format="json", HTTP_X_API_KEY="nope").status_code == 401
        assert client.post("/wallets", {"currency": "USD"}, format="json", HTTP_X_API_KEY="k-user").status_code in (201, 409)
        assert client.get("/admin/flags", HTTP_X_API_KEY="k-user").status_code == 403
        assert client.get("/admin/flags", HTTP_X_API_KEY="k-admin").status_code == 200


class TestWallets:
    def test_one_wallet_per_user_and_currency(self):
        o = u("w-1")
        wallet(o, "USD")
        assert call(user(o), "POST", "/wallets", {"currency": "USD"}).status == 409
        assert call(user(o), "POST", "/wallets", {"currency": "EUR"}).status == 201

    def test_balance_is_a_string_of_minor_units(self):
        o = u("w-2")
        w = funded_wallet(o, 12345)
        r = call(user(o), "GET", f"/wallets/{w}")
        assert r.body["balance"] == "12345"
        assert r.body["status"] == "active"

    @pytest.mark.parametrize("amount", [1.5, "1.50", -5, 0, "abc", True, "1e3", None])
    def test_rejects_non_integer_or_float_amounts(self, amount):
        o = u("w-3")
        w = wallet(o)
        r = call(user(o), "POST", f"/wallets/{w}/topups", {"amount": amount, "payment_method": "tok"}, key())
        assert r.status == 400

    def test_bad_uuid_and_unknown_wallet(self):
        assert call(ADMIN, "GET", "/wallets/not-a-uuid").status == 400
        assert call(ADMIN, "GET", f"/wallets/{uuid.uuid4()}").status == 404


class TestListWallets:
    def test_users_list_only_their_own_wallets_admins_list_all(self):
        a, b = u("ls-a"), u("ls-b")
        wa, wb = wallet(a), wallet(b)
        mine = call(user(a), "GET", "/wallets").body["items"]
        assert [w["id"] for w in mine] == [wa]
        everyone = {w["id"] for w in call(ADMIN, "GET", "/wallets").body["items"]}
        assert {wa, wb} <= everyone
        assert call(None, "GET", "/wallets").status == 401

    def test_system_wallets_are_never_listed(self):
        funded_wallet(u("ls-c"), 100)  # creates system:external
        owners = {w["owner_id"] for w in call(ADMIN, "GET", "/wallets").body["items"]}
        assert not any(o.startswith("system:") for o in owners)


class TestTopUp:
    def test_credits_the_wallet(self):
        o = u("tu-1")
        w = wallet(o)
        r = call(user(o), "POST", f"/wallets/{w}/topups", {"amount": 5000, "payment_method": "tok_visa"}, key())
        assert r.status == 201
        assert balance(w) == 5000

    def test_declined_payments_move_no_money(self):
        o = u("tu-2")
        w = wallet(o)
        r = call(user(o), "POST", f"/wallets/{w}/topups", {"amount": 5000, "payment_method": "tok_decline"}, key())
        assert r.status == 402
        assert balance(w) == 0

    def test_requires_idempotency_key(self):
        o = u("tu-3")
        w = wallet(o)
        assert call(user(o), "POST", f"/wallets/{w}/topups", {"amount": 5000, "payment_method": "tok_visa"}).status == 400


class TestTransfers:
    def test_moves_money_atomically_and_never_overdraws(self):
        a, b = u("tr-a"), u("tr-b")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        assert transfer(user(a), wa, wb, 400).status == 201
        assert (balance(wa), balance(wb)) == (600, 400)
        over = transfer(user(a), wa, wb, 601)
        assert over.status == 422 and over.body["error"]["code"] == "insufficient_funds"
        assert balance(wa) == 600

    def test_cannot_spend_from_someone_elses_wallet(self):
        a, b = u("tr-c"), u("tr-d")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        assert transfer(user(b), wa, wb, 10).status == 403

    def test_rejects_self_transfer_unknown_wallet_and_cross_currency(self):
        e = u("tr-e")
        wa = funded_wallet(e, 1000)
        assert transfer(user(e), wa, wa, 10).status == 400
        assert transfer(user(e), wa, uuid.uuid4(), 10).status == 404
        eur = wallet(u("tr-f"), "EUR")
        assert transfer(user(e), wa, eur, 10).body["error"]["code"] == "currency_mismatch"

    def test_daily_limit_and_admin_can_change_it(self):
        a, b = u("dl-a"), u("dl-b")
        wa, wb = funded_wallet(a, 10_000), wallet(b)
        assert call(ADMIN, "PUT", f"/admin/wallets/{wa}/daily-limit", {"daily_limit": 1000}).status == 200
        assert transfer(user(a), wa, wb, 700).status == 201
        r = transfer(user(a), wa, wb, 301)
        assert r.status == 422 and r.body["error"]["code"] == "daily_limit_exceeded"
        assert transfer(user(a), wa, wb, 300).status == 201
        cleared = call(ADMIN, "PUT", f"/admin/wallets/{wa}/daily-limit", {"daily_limit": None})
        assert cleared.body["daily_limit"] is None


class TestIdempotency:
    def test_replays_the_original_result_and_moves_money_once(self):
        a, b = u("id-a"), u("id-b")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        k = str(uuid.uuid4())
        first = transfer(user(a), wa, wb, 300, k)
        second = transfer(user(a), wa, wb, 300, k)
        assert second.status == first.status
        assert second.body["transfer_id"] == first.body["transfer_id"]
        assert second.headers["Idempotent-Replayed"] == "true"
        assert balance(wa) == 700

    def test_rejects_reuse_of_a_key_with_a_different_payload(self):
        a, b = u("id-c"), u("id-d")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        k = str(uuid.uuid4())
        transfer(user(a), wa, wb, 100, k)
        r = transfer(user(a), wa, wb, 200, k)
        assert r.status == 422 and r.body["error"]["code"] == "idempotency_key_reuse"
        assert balance(wa) == 900

    def test_replays_a_stored_rejection_too(self):
        a, b = u("id-e"), u("id-f")
        wa, wb = funded_wallet(a, 100), wallet(b)
        k = str(uuid.uuid4())
        assert transfer(user(a), wa, wb, 500, k).status == 422
        call(user(a), "POST", f"/wallets/{wa}/topups", {"amount": 1000, "payment_method": "t"}, key())
        assert transfer(user(a), wa, wb, 500, k).status == 422  # same key => same answer
        assert transfer(user(a), wa, wb, 500).status == 201  # new key => evaluated afresh

    def test_keys_are_scoped_per_caller(self):
        a, b = u("id-g"), u("id-h")
        wa, wb = funded_wallet(a, 1000), funded_wallet(b, 1000)
        k = str(uuid.uuid4())
        assert transfer(user(a), wa, wb, 100, k).status == 201
        assert transfer(user(b), wb, wa, 100, k).status == 201


class TestStatement:
    def test_paginates_newest_first_and_filters_by_date(self):
        a, b = u("st-a"), u("st-b")
        wa, wb = funded_wallet(a, 100_000), wallet(b)
        for i in range(1, 8):
            transfer(user(a), wa, wb, i)
        seen, cursor, pages = [], None, 0
        while True:
            url = f"/wallets/{wa}/transactions?limit=3" + (f"&cursor={cursor}" if cursor else "")
            r = call(user(a), "GET", url)
            assert r.status == 200
            seen += [i["entry_id"] for i in r.body["items"]]
            cursor, pages = r.body["next_cursor"], pages + 1
            if not cursor:
                break
        assert len(seen) == 8 and len(set(seen)) == 8 and pages == 3  # 1 top-up + 7 transfers

        first = call(user(a), "GET", f"/wallets/{wa}/transactions?limit=1").body["items"][0]
        assert (first["direction"], first["type"], first["amount"], first["counterparty_wallet_id"]) == (
            "debit", "transfer", "7", wb)

        future = quote((timezone.now() + timedelta(hours=1)).isoformat())
        assert call(user(a), "GET", f"/wallets/{wa}/transactions?from={future}").body["items"] == []
        assert len(call(user(a), "GET", f"/wallets/{wa}/transactions?to={future}").body["items"]) == 8

    def test_rejects_a_bad_cursor(self):
        o = u("st-c")
        w = wallet(o)
        assert call(user(o), "GET", f"/wallets/{w}/transactions?cursor=abc").status == 400


class TestReversals:
    def test_reverses_with_compensating_entries_once_never_editing_history(self):
        a, b = u("rv-a"), u("rv-b")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        tr = transfer(user(a), wa, wb, 400)
        tid = tr.body["transfer_id"]
        before = sql("SELECT count(*) FROM ledger_entries")[0][0]

        assert call(user(a), "POST", f"/admin/transfers/{tid}/reverse", {"reason": "nope nope"}).status == 403
        rev = call(ADMIN, "POST", f"/admin/transfers/{tid}/reverse", {"reason": "customer dispute"})
        assert rev.status == 201
        assert (balance(wa), balance(wb)) == (1000, 0)
        assert sql("SELECT count(*) FROM ledger_entries")[0][0] == before + 2

        again = call(ADMIN, "POST", f"/admin/transfers/{tid}/reverse", {"reason": "second try"})
        assert again.status == 409 and again.body["error"]["code"] == "already_reversed"
        assert reconcile_ok()

    def test_refuses_to_reverse_when_the_destination_has_spent_the_money(self):
        a, b, c = u("rv-c"), u("rv-d"), u("rv-e")
        wa, wb, wc = funded_wallet(a, 1000), wallet(b), wallet(c)
        tr = transfer(user(a), wa, wb, 400)
        transfer(user(b), wb, wc, 300)
        r = call(ADMIN, "POST", f"/admin/transfers/{tr.body['transfer_id']}/reverse", {"reason": "too late"})
        assert r.status == 409
        assert balance(wb) == 100

    def test_only_transfers_can_be_reversed(self):
        o = u("rv-t")
        w = funded_wallet(o, 100)
        txn = call(user(o), "GET", f"/wallets/{w}/transactions").body["items"][0]["transaction_id"]
        r = call(ADMIN, "POST", f"/admin/transfers/{txn}/reverse", {"reason": "not a transfer"})
        assert r.status == 422 and r.body["error"]["code"] == "not_a_transfer"

    def test_frees_the_daily_limit_used_by_the_reversed_transfer(self):
        a, b = u("rv-f"), u("rv-g")
        wa, wb = funded_wallet(a, 1000), wallet(b)
        call(ADMIN, "PUT", f"/admin/wallets/{wa}/daily-limit", {"daily_limit": 500})
        tr = transfer(user(a), wa, wb, 500)
        assert transfer(user(a), wa, wb, 1).status == 422
        call(ADMIN, "POST", f"/admin/transfers/{tr.body['transfer_id']}/reverse", {"reason": "mistake"})
        assert transfer(user(a), wa, wb, 500).status == 201

    def test_ledger_rows_cannot_be_updated_or_deleted_even_with_raw_sql(self):
        from django.db import DatabaseError

        for stmt in ("UPDATE ledger_entries SET amount = 1", "DELETE FROM transactions", "TRUNCATE ledger_entries CASCADE"):
            with pytest.raises(DatabaseError, match="append-only"):
                sql(stmt)


class TestWalletStates:
    def test_frozen_wallets_receive_but_cannot_send_and_history_is_audited(self):
        a, b = u("ws-a"), u("ws-b")
        wa, wb = funded_wallet(a, 1000), funded_wallet(b, 1000)
        assert call(ADMIN, "POST", f"/admin/wallets/{wa}/freeze", {"reason": "suspicious"}).status == 200
        blocked = transfer(user(a), wa, wb, 10)
        assert blocked.status == 409 and blocked.body["error"]["code"] == "wallet_frozen"
        assert transfer(user(b), wb, wa, 10).status == 201  # can receive
        assert call(user(a), "POST", f"/wallets/{wa}/topups", {"amount": 5, "payment_method": "t"}, key()).status == 201
        assert call(ADMIN, "POST", f"/admin/wallets/{wa}/freeze", {"reason": "again"}).status == 409
        assert call(ADMIN, "POST", f"/admin/wallets/{wa}/unfreeze", {"reason": "cleared"}).status == 200
        assert transfer(user(a), wa, wb, 10).status == 201
        h = call(ADMIN, "GET", f"/admin/wallets/{wa}/state-history").body["items"]
        assert [[i["from_status"], i["to_status"], i["reason"], i["actor"]] for i in h] == [
            ["active", "frozen", "suspicious", "admin-1"],
            ["frozen", "active", "cleared", "admin-1"],
        ]

    def test_close_needs_zero_balance_closed_wallets_accept_nothing_and_cannot_reopen(self):
        a, b = u("ws-c"), u("ws-d")
        wa, wb = funded_wallet(a, 100), wallet(b)
        no = call(ADMIN, "POST", f"/admin/wallets/{wa}/close", {"reason": "user left"})
        assert no.status == 409 and no.body["error"]["code"] == "balance_not_zero"
        transfer(user(a), wa, wb, 100)
        assert call(ADMIN, "POST", f"/admin/wallets/{wa}/close", {"reason": "user left"}).status == 200
        assert transfer(user(b), wb, wa, 10).body["error"]["code"] == "destination_closed"
        assert call(user(a), "POST", f"/wallets/{wa}/topups", {"amount": 5, "payment_method": "t"}, key()).status == 409
        assert call(ADMIN, "POST", f"/admin/wallets/{wa}/unfreeze", {"reason": "reopen"}).status == 409

    def test_state_changes_require_a_reason(self):
        w = wallet(u("ws-e"))
        assert call(ADMIN, "POST", f"/admin/wallets/{w}/freeze", {}).status == 400


class TestSwagger:
    def test_serves_the_openapi_document(self):
        r = APIClient().get("/docs/json")
        assert r.status_code == 200
        paths = set(json.loads(r.content)["paths"])
        assert {"/transfers", "/admin/flags", "/bank/callbacks"} <= paths
        assert any(p.endswith("/withdrawals") and p.startswith("/wallets/") for p in paths)
        assert APIClient().get("/docs").status_code == 200
