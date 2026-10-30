from django.db import connection

from ..types import Rule, RuleContext, RuleHit
from .params import amount_param, int_param


def withdrawal_after_topup_rule(cfg: dict) -> Rule:
    """A withdrawal within `windowMinutes` of a single top-up of at least `topupAtLeast`."""
    at_least = amount_param(cfg, "topupAtLeast")
    window = int_param(cfg, "windowMinutes")

    def evaluate(ctx: RuleContext) -> RuleHit | None:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT MAX(amount) FROM transactions
                    WHERE type = 'topup' AND dest_wallet_id = %s
                      AND created_at > clock_timestamp() - make_interval(mins => %s)""",
                [ctx.wallet.id, window],
            )
            largest = cur.fetchone()[0]
        if largest is not None and largest >= at_least:
            return RuleHit(
                f"withdrawal within {window} min of a top-up >= {at_least}",
                {"largestTopup": str(largest), "windowMinutes": window},
            )
        return None

    return Rule(cfg["id"], cfg["type"], cfg["action"], cfg.get("appliesTo") or ["withdrawal"], evaluate)
