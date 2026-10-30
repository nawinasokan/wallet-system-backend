"""Schema. Money is BIGINT minor units. Ledger tables are append-only (enforced by DB triggers, see
migrations/0002_integrity.py) and every transaction's debits must equal its credits at commit."""
import uuid

from django.db import models
from django.db.models import Q
from django.db.models.fields.json import KeyTextTransform

from .dbfuncs import ClockTimestamp


def _now():
    return ClockTimestamp()


class Wallet(models.Model):
    class Kind(models.TextChoices):
        USER = "user"
        SYSTEM = "system"  # ledger counter-parties: the outside world, withdrawal hold

    class Status(models.TextChoices):
        ACTIVE = "active"
        FROZEN = "frozen"
        CLOSED = "closed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_id = models.TextField()
    currency = models.CharField(max_length=3)
    # system wallets' balance is NOT maintained (avoids a hot row); derive it from the ledger
    kind = models.TextField(choices=Kind.choices, default=Kind.USER)
    balance = models.BigIntegerField(default=0)
    status = models.TextField(choices=Status.choices, default=Status.ACTIVE)
    status_reason = models.TextField(null=True, blank=True)
    daily_limit = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "wallets"
        constraints = [
            models.UniqueConstraint(fields=["owner_id", "currency"], name="wallets_owner_currency_uniq"),
            models.CheckConstraint(condition=Q(currency__regex=r"^[A-Z]{3}$"), name="wallets_currency_fmt"),
            models.CheckConstraint(condition=Q(kind__in=["user", "system"]), name="wallets_kind_valid"),
            models.CheckConstraint(
                condition=Q(status__in=["active", "frozen", "closed"]), name="wallets_status_valid"
            ),
            models.CheckConstraint(
                condition=Q(daily_limit__isnull=True) | Q(daily_limit__gte=0), name="wallets_daily_limit_nonneg"
            ),
            # last line of defence: a user balance can never go negative
            models.CheckConstraint(
                condition=Q(kind="system") | Q(balance__gte=0), name="balance_non_negative"
            ),
        ]


class WalletStateHistory(models.Model):
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="state_history")
    from_status = models.TextField()
    to_status = models.TextField()
    reason = models.TextField()
    actor = models.TextField()
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "wallet_state_history"
        indexes = [models.Index(fields=["wallet", "id"], name="wallet_state_history_wallet")]


class Transaction(models.Model):
    """One row per business movement (a "journal"). Immutable."""

    class Type(models.TextChoices):
        TOPUP = "topup"
        TRANSFER = "transfer"
        WITHDRAWAL = "withdrawal"
        WITHDRAWAL_SETTLE = "withdrawal_settle"
        WITHDRAWAL_REFUND = "withdrawal_refund"
        REVERSAL = "reversal"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.TextField(choices=Type.choices)
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    source_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="+")
    dest_wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="+")
    # compensating transactions point at what they undo; UNIQUE => undone at most once, ever
    reverses = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversal"
    )
    reason = models.TextField(null=True, blank=True)
    created_by = models.TextField(null=True, blank=True)
    metadata = models.JSONField(default=dict)
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "transactions"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="transactions_amount_positive"),
            models.CheckConstraint(
                condition=~Q(source_wallet=models.F("dest_wallet")), name="transactions_distinct_wallets"
            ),
            models.CheckConstraint(
                condition=Q(
                    type__in=[
                        "topup", "transfer", "withdrawal", "withdrawal_settle", "withdrawal_refund", "reversal",
                    ]
                ),
                name="transactions_type_valid",
            ),
            models.UniqueConstraint(
                KeyTextTransform("withdrawal_id", "metadata"),
                condition=Q(type="withdrawal_settle"),
                name="transactions_one_settle_per_withdrawal",
            ),
        ]
        indexes = [
            models.Index(fields=["source_wallet", "type", "created_at"], name="transactions_source"),
            models.Index(fields=["dest_wallet", "type", "created_at"], name="transactions_dest"),
        ]


class LedgerEntry(models.Model):
    """Double entry: every transaction has one debit and one credit of the same amount. Immutable.
    Wallet balance = SUM(credits) - SUM(debits)."""

    class Direction(models.TextChoices):
        DEBIT = "debit"
        CREDIT = "credit"

    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="entries")
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="entries")
    direction = models.TextField(choices=Direction.choices)
    amount = models.BigIntegerField()
    balance_after = models.BigIntegerField(null=True)  # running balance for user wallets
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "ledger_entries"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="ledger_entries_amount_positive"),
            models.CheckConstraint(
                condition=Q(direction__in=["debit", "credit"]), name="ledger_entries_direction_valid"
            ),
        ]
        indexes = [
            models.Index(models.F("wallet"), models.F("id").desc(), name="ledger_entries_wallet"),
            models.Index(fields=["wallet", "created_at"], name="ledger_entries_wallet_time"),
        ]


class Withdrawal(models.Model):
    class Status(models.TextChoices):
        REQUESTED = "requested"
        PROCESSING = "processing"
        COMPLETED = "completed"
        FAILED = "failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="withdrawals")
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    destination = models.JSONField()
    status = models.TextField(choices=Status.choices)
    failure_reason = models.TextField(null=True, blank=True)
    bank_reference = models.TextField(null=True, blank=True)
    hold_txn = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="+")
    resolution_txn = models.ForeignKey(
        Transaction, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.TextField()
    created_at = models.DateTimeField(db_default=_now())
    updated_at = models.DateTimeField(db_default=_now())
    processing_started_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "withdrawals"
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="withdrawals_amount_positive"),
            models.CheckConstraint(
                condition=Q(status__in=["requested", "processing", "completed", "failed"]),
                name="withdrawals_status_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "created_at"], name="withdrawals_status"),
            models.Index(fields=["wallet", "status"], name="withdrawals_wallet"),
        ]


class BankCallback(models.Model):
    event_id = models.TextField(primary_key=True)  # provider event id: dedupes redelivery
    withdrawal_id = models.UUIDField(null=True)
    payload = models.JSONField()
    outcome = models.TextField()  # applied | ignored_terminal | conflict | unknown_withdrawal
    received_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "bank_callbacks"


class IdempotencyKey(models.Model):
    principal = models.TextField()
    key = models.TextField()
    request_hash = models.TextField()
    status_code = models.IntegerField(null=True)
    response = models.JSONField(null=True)
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "idempotency_keys"
        constraints = [models.UniqueConstraint(fields=["principal", "key"], name="idempotency_principal_key")]


class RiskDecision(models.Model):
    """Every risk-engine verdict, including allows. Immutable."""

    subject_type = models.TextField()  # transfer | withdrawal
    subject_id = models.UUIDField()  # id the transfer/withdrawal has (or would have had if blocked)
    wallet = models.ForeignKey(Wallet, on_delete=models.PROTECT, related_name="+")
    amount = models.BigIntegerField()
    currency = models.CharField(max_length=3)
    decision = models.TextField()  # allow | flag | block
    rule_id = models.TextField(null=True)  # NULL only for a plain allow
    rule_type = models.TextField(null=True)
    reason = models.TextField(null=True)
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(db_default=_now())

    class Meta:
        db_table = "risk_decisions"
        constraints = [
            models.CheckConstraint(
                condition=Q(subject_type__in=["transfer", "withdrawal"]), name="risk_subject_valid"
            ),
            models.CheckConstraint(
                condition=Q(decision__in=["allow", "flag", "block"]), name="risk_decision_valid"
            ),
        ]
        indexes = [
            models.Index(models.F("decision"), models.F("id").desc(), name="risk_decisions_decision"),
            models.Index(models.F("wallet"), models.F("id").desc(), name="risk_decisions_wallet"),
        ]
