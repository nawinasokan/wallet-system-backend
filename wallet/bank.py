import hashlib
import hmac
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

import requests


def sign_callback(secret: str, raw_body: bytes | str) -> str:
    raw = raw_body.encode() if isinstance(raw_body, str) else raw_body
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@dataclass
class SubmitResult:
    accepted: bool
    reference: str | None = None
    reason: str | None = None


# (payload, delay_ms) -> None
CallbackSender = Callable[[dict, int], None]


class MockBank:
    """Mock bank. Outcome is chosen by the destination account number suffix:
        ...1111  rejects the payment synchronously at submit
        ...0000  accepts, then reports `failed` asynchronously
        ...9999  accepts, then never calls back (timeout)
        ...7777  accepts, reports `completed` several times (duplicate + redelivered callbacks)
        else     accepts, reports `completed` once
    Callbacks go to `send` (Celery task or HTTP POST in the app, a collector in tests)."""

    def __init__(self, send: CallbackSender, delay_ms: int = 0):
        self.send = send
        self.delay_ms = delay_ms

    def submit(self, withdrawal_id, destination: dict) -> SubmitResult:
        acct = destination["account_number"]
        reference = f"bank_{uuid.uuid4().hex[:12]}"
        if acct.endswith("1111"):
            return SubmitResult(False, reason="account_closed")
        base = {"withdrawal_id": str(withdrawal_id), "bank_reference": reference}

        def emit(payload: dict, extra_delay: int = 0):
            self.send(payload, self.delay_ms + extra_delay)

        def evt() -> str:
            return f"evt_{uuid.uuid4()}"

        if acct.endswith("9999"):
            return SubmitResult(True, reference)
        if acct.endswith("0000"):
            emit({**base, "event_id": evt(), "status": "failed", "failure_reason": "insufficient_beneficiary_details"})
        elif acct.endswith("7777"):
            same = evt()
            emit({**base, "event_id": same, "status": "completed"})
            emit({**base, "event_id": same, "status": "completed"}, 5)  # exact redelivery
            emit({**base, "event_id": evt(), "status": "completed"}, 10)  # new event, same outcome
        else:
            emit({**base, "event_id": evt(), "status": "completed"})
        return SubmitResult(True, reference)


def post_signed_callback(url: str, secret: str, payload: dict) -> None:
    """POST a signed callback to the API, like a real bank webhook."""
    raw = json.dumps(payload)
    requests.post(
        url,
        data=raw,
        headers={"Content-Type": "application/json", "X-Bank-Signature": sign_callback(secret, raw)},
        timeout=10,
    )


def thread_callback_sender(url: str, secret: str) -> CallbackSender:
    """Used by the standalone `run_worker` command (no Celery): delay with a daemon timer."""

    def send(payload: dict, delay_ms: int) -> None:
        def go():
            try:
                post_signed_callback(url, secret, payload)
            except Exception:
                pass

        t = threading.Timer(delay_ms / 1000, go)
        t.daemon = True
        t.start()

    return send


def celery_callback_sender() -> CallbackSender:
    from .tasks import deliver_bank_callback

    def send(payload: dict, delay_ms: int) -> None:
        deliver_bank_callback.apply_async(args=[payload], countdown=delay_ms / 1000)

    return send
