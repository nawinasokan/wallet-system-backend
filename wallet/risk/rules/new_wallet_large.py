from django.db import connection

from ..types import Rule, RuleContext, RuleHit
from .params import amount_param, int_param


def new_wallet_large_rule(cfg: dict) -> Rule:
    """A transfer or withdrawal above `amountAbove` by a wallet younger than `walletAgeDays`."""
    above = amount_param(cfg, "amountAbove")
    age_days = int_param(cfg, "walletAgeDays")

    def evaluate(ctx: RuleContext) -> RuleHit | None:
        if ctx.amount <= above:
            return None
        with connection.cursor() as cur:
            cur.execute(
                "SELECT (clock_timestamp() - created_at) < make_interval(days => %s) FROM wallets WHERE id = %s",
                [age_days, ctx.wallet.id],
            )
            row = cur.fetchone()
        if not row or not row[0]:
            return None
        return RuleHit(
            f"amount above {above} from wallet younger than {age_days} days",
            {"amountAbove": above, "walletAgeDays": age_days},
        )

    return Rule(cfg["id"], cfg["type"], cfg["action"], cfg.get("appliesTo") or ["transfer", "withdrawal"], evaluate)
