"""Request validation + OpenAPI schema. Validation failures become 400 `validation_error`;
bad amounts become 400 `invalid_amount`."""
from rest_framework import serializers

from .errors import ApiError
from .money import parse_amount


class AmountField(serializers.Field):
    """Positive integer in minor units (integer or digit string). Never a decimal."""

    default_error_messages = {}

    def to_internal_value(self, data):
        try:
            return parse_amount(data)
        except (TypeError, ValueError) as e:
            raise ApiError(400, "invalid_amount", str(e)) from None

    def to_representation(self, value):
        return str(value)


class NullableAmountField(AmountField):
    """Like AmountField but 0 is allowed (a limit of zero blocks all outgoing money)."""

    def to_internal_value(self, data):
        if isinstance(data, str) and data.isdigit() and int(data) == 0 or data == 0 and not isinstance(data, bool):
            return 0
        return super().to_internal_value(data)


class StrictSerializer(serializers.Serializer):
    """Rejects unknown fields (additionalProperties: false)."""

    def to_internal_value(self, data):
        if isinstance(data, dict):
            extra = set(data) - set(self.fields)
            if extra:
                raise serializers.ValidationError({k: "Unknown field" for k in sorted(extra)})
        return super().to_internal_value(data)


class DevTokenIn(StrictSerializer):
    sub = serializers.CharField(min_length=1)
    role = serializers.ChoiceField(choices=["user", "admin"], default="user")


class CreateWalletIn(StrictSerializer):
    currency = serializers.RegexField(r"^[A-Z]{3}$", help_text="ISO 4217 code")
    owner_id = serializers.CharField(min_length=1, required=False, help_text="Admin only: create for another user")


class TopUpIn(StrictSerializer):
    amount = AmountField()
    payment_method = serializers.CharField(min_length=1)


class TransferIn(StrictSerializer):
    source_wallet_id = serializers.UUIDField()
    destination_wallet_id = serializers.UUIDField()
    amount = AmountField()
    description = serializers.CharField(max_length=200, required=False, allow_null=False)


class DestinationIn(StrictSerializer):
    account_number = serializers.RegexField(r"^[0-9]{6,20}$")
    account_holder = serializers.CharField(min_length=1, max_length=100)
    bank_code = serializers.CharField(min_length=2, max_length=20)


class WithdrawalIn(StrictSerializer):
    amount = AmountField()
    destination = DestinationIn()


class BankCallbackIn(StrictSerializer):
    event_id = serializers.CharField(min_length=1)
    withdrawal_id = serializers.UUIDField()
    bank_reference = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=["completed", "failed"])
    failure_reason = serializers.CharField(required=False)


class ReasonIn(StrictSerializer):
    reason = serializers.CharField(min_length=3, max_length=500)


class DailyLimitIn(StrictSerializer):
    daily_limit = NullableAmountField(allow_null=True)


class StatementQuery(serializers.Serializer):
    to = serializers.DateTimeField(required=False, help_text="exclusive")
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)
    cursor = serializers.CharField(required=False)

    def get_fields(self):
        fields = super().get_fields()
        # `from` is a Python keyword, so it can't be a class attribute
        fields["from"] = serializers.DateTimeField(required=False, source="from_", help_text="inclusive")
        return fields


class FlagsQuery(serializers.Serializer):
    decision = serializers.ChoiceField(choices=["flag", "block", "all"], default="flag")
    limit = serializers.IntegerField(min_value=1, max_value=200, default=50)
    cursor = serializers.RegexField(r"^[0-9]{1,18}$", required=False)
