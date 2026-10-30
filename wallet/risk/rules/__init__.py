from ..types import RuleFactory
from .new_wallet_large import new_wallet_large_rule
from .velocity import velocity_rule
from .withdrawal_after_topup import withdrawal_after_topup_rule

# Rule type registry. To add a rule: write a factory in this package, register it here, then
# reference its `type` from rules/rules.json. Transfer/withdrawal code is never touched.
RULE_TYPES: dict[str, RuleFactory] = {
    "velocity_outgoing_transfers": velocity_rule,
    "new_wallet_large_amount": new_wallet_large_rule,
    "withdrawal_after_large_topup": withdrawal_after_topup_rule,
}
