import binascii
from dataclasses import dataclass


@dataclass
class ChargeResult:
    ok: bool
    reference: str | None = None
    reason: str | None = None


def charge_mock_provider(*, amount: int, currency: str, payment_method: str, idempotency_key: str) -> ChargeResult:
    """Mock payment provider. Behaviour is driven by the payment method token:
        tok_decline       -> card declined
        tok_insufficient  -> insufficient funds on the card
        anything else     -> succeeds
    The reference is derived from the idempotency key, as a real provider would dedupe on it."""
    if payment_method == "tok_decline":
        return ChargeResult(False, reason="card_declined")
    if payment_method == "tok_insufficient":
        return ChargeResult(False, reason="insufficient_funds_on_card")
    ref = binascii.hexlify(idempotency_key.encode()).decode()[:24]
    return ChargeResult(True, reference=f"pay_{ref}")
