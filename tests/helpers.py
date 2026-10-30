import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from django.db import connection, connections
from rest_framework.test import APIClient

from wallet.auth import Actor, sign_token
from wallet.bank import MockBank, sign_callback
from wallet.services.reconciliation import reconcile
from wallet.services.withdrawals import submit_pending, sweep_timeouts
from django.conf import settings

ADMIN = Actor("admin-1", "admin")


def user(owner: str) -> Actor:
    return Actor(owner, "user")


def u(name: str) -> str:
    """Unique owner id: the test database is shared by a whole run, so every test namespaces its users."""
    return f"{name}-{uuid.uuid4().hex[:8]}"


@dataclass
class Resp:
    status: int
    body: dict | None
    headers: dict


def call(actor: Actor | None, method: str, url: str, body=None, headers: dict | None = None) -> Resp:
    extra = {f"HTTP_{k.upper().replace('-', '_')}": v for k, v in (headers or {}).items()}
    if actor:
        extra["HTTP_AUTHORIZATION"] = f"Bearer {sign_token(actor)}"
    kwargs = {"content_type": "application/json", **extra}
    if body is not None:
        kwargs["data"] = json.dumps(body)
    r = APIClient().generic(method, url, **kwargs)
    return Resp(r.status_code, json.loads(r.content) if r.content else None, dict(r.headers))


def key() -> dict:
    return {"idempotency-key": str(uuid.uuid4())}


def wallet(owner: str, currency: str = "USD") -> str:
    r = call(user(owner), "POST", "/wallets", {"currency": currency})
    assert r.status == 201, r.body
    return r.body["id"]


def funded_wallet(owner: str, amount: int, currency: str = "USD") -> str:
    wid = wallet(owner, currency)
    r = call(user(owner), "POST", f"/wallets/{wid}/topups", {"amount": str(amount), "payment_method": "tok_visa"}, key())
    assert r.status == 201, r.body
    return wid


def transfer(actor: Actor, src, dst, amount, idem: str | None = None) -> Resp:
    return call(
        actor, "POST", "/transfers",
        {"source_wallet_id": str(src), "destination_wallet_id": str(dst), "amount": str(amount)},
        {"idempotency-key": idem or str(uuid.uuid4())},
    )


def balance(wallet_id) -> int:
    with connection.cursor() as cur:
        cur.execute("SELECT balance FROM wallets WHERE id = %s", [wallet_id])
        return cur.fetchone()[0]


def sql(query: str, params=None):
    with connection.cursor() as cur:
        cur.execute(query, params or [])
        return cur.fetchall() if cur.description else None


def parallel(fns, workers: int = 30):
    """Run callables on threads (each with its own DB connection, closed afterwards)."""

    def wrap(fn):
        try:
            return fn()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(wrap, fns))


def statuses(rs) -> dict:
    out: dict = {}
    for r in rs:
        out[r.status] = out.get(r.status, 0) + 1
    return out


def reconcile_ok() -> bool:
    return reconcile()["ok"]


class Bank:
    """A mock bank whose callbacks are collected instead of POSTed (tests deliver them by hand)."""

    def __init__(self):
        self.outbox: list[dict] = []
        self.bank = MockBank(lambda payload, delay: self.outbox.append(dict(payload)), 0)

    def run_worker(self):
        return submit_pending(self.bank), sweep_timeouts()


def bank_callback(payload: dict) -> Resp:
    raw = json.dumps(payload)
    r = APIClient().generic(
        "POST", "/bank/callbacks", data=raw, content_type="application/json",
        HTTP_X_BANK_SIGNATURE=sign_callback(settings.BANK_WEBHOOK_SECRET, raw),
    )
    return Resp(r.status_code, json.loads(r.content) if r.content else None, dict(r.headers))
