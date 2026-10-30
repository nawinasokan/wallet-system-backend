from django.db import connection

from ..types import Rule, RuleContext, RuleHit
from .params import int_param


def velocity_rule(cfg: dict) -> Rule:
    """More than `maxTransfers` outgoing transfers within `windowMinutes`."""
    max_transfers = int_param(cfg, "maxTransfers")
    window = int_param(cfg, "windowMinutes")

    def evaluate(ctx: RuleContext) -> RuleHit | None:
        with connection.cursor() as cur:
            cur.execute(
                """SELECT count(*) FROM transactions
                    WHERE type = 'transfer' AND source_wallet_id = %s
                      AND created_at > clock_timestamp() - make_interval(mins => %s)""",
                [ctx.wallet.id, window],
            )
            prior = cur.fetchone()[0]
        # this request would be number prior+1
        if prior + 1 > max_transfers:
            return RuleHit(
                f"more than {max_transfers} outgoing transfers in {window} min",
                {"prior": prior, "max": max_transfers, "windowMinutes": window},
            )
        return None

    return Rule(cfg["id"], cfg["type"], cfg["action"], cfg.get("appliesTo") or ["transfer"], evaluate)
