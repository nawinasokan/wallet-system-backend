from ..auth import Actor, assert_can_access
from ..errors import ApiError, Result, ok, reject
from ..models import Wallet
from ..money import m
from ..payments import charge_mock_provider
from .ledger import lock_wallets, post_journal, system_wallet


def top_up(actor: Actor, wallet_id, amount: int, payment_method: str, idempotency_key: str) -> Result:
    wallet = lock_wallets([wallet_id]).get(wallet_id)
    if wallet is None:
        raise ApiError(404, "wallet_not_found", "Wallet not found")
    assert_can_access(actor, wallet)
    # Frozen wallets may receive funds; closed wallets accept nothing.
    if wallet.status == Wallet.Status.CLOSED:
        return reject(409, "wallet_closed", "Wallet is closed")

    # Trade-off: the (mock, in-process) provider is called while holding the wallet lock so the
    # closed/frozen check and the credit are atomic. A real, slow provider would use a
    # pending-top-up state + webhook instead.
    charge = charge_mock_provider(
        amount=amount, currency=wallet.currency, payment_method=payment_method, idempotency_key=idempotency_key
    )
    if not charge.ok:
        return reject(402, "payment_declined", f"Payment provider declined: {charge.reason}")

    external = system_wallet("external", wallet.currency)
    j = post_journal(
        type="topup",
        amount=amount,
        source=external,
        dest=wallet,
        created_by=actor.id,
        metadata={"payment_reference": charge.reference},
    )
    return ok(
        201,
        {
            "transaction_id": j.id,
            "type": "topup",
            "wallet_id": wallet.id,
            "amount": m(amount),
            "currency": wallet.currency,
            "balance": m(wallet.balance + amount),
            "payment_reference": charge.reference,
            "created_at": j.created_at,
        },
    )
