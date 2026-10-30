import json
from dataclasses import dataclass, field

from django.conf import settings

from ..models import RiskDecision
from .rules import RULE_TYPES
from .types import Rule, RuleContext, RuleFactory

SEVERITY = {"allow": 0, "flag": 1, "block": 2}


@dataclass
class Verdict:
    decision: str
    triggered: list[dict] = field(default_factory=list)  # {rule_id, rule_type, decision, reason}

    def rules_payload(self) -> list[dict]:
        return [{"id": t["rule_id"], "reason": t["reason"]} for t in self.triggered]


class RiskEngine:
    def __init__(self, rules: list[Rule]):
        self.rules = rules

    @classmethod
    def from_configs(cls, configs: list[dict], registry: dict[str, RuleFactory] | None = None) -> "RiskEngine":
        registry = registry or RULE_TYPES
        seen: set[str] = set()
        rules = []
        for c in configs:
            if c.get("enabled") is False:
                continue
            factory = registry.get(c["type"])
            if factory is None:
                raise ValueError(f'Unknown risk rule type "{c["type"]}" (rule {c["id"]})')
            if c["id"] in seen:
                raise ValueError(f'Duplicate risk rule id "{c["id"]}"')
            seen.add(c["id"])
            rules.append(factory(c))
        return cls(rules)

    @classmethod
    def from_file(cls, path: str) -> "RiskEngine":
        with open(path) as f:
            return cls.from_configs(json.load(f)["rules"])

    def evaluate(self, ctx: RuleContext, subject_id) -> Verdict:
        """Evaluate every applicable rule and persist every verdict (one row per triggered rule,
        or a single `allow` row). Runs on the caller's transaction while it holds the sender's
        wallet lock, so the counts rules read cannot be raced by a parallel request."""
        triggered = []
        for rule in self.rules:
            if ctx.subject not in rule.applies_to:
                continue
            hit = rule.evaluate(ctx)
            if hit:
                triggered.append(
                    {"rule_id": rule.id, "rule_type": rule.type, "decision": rule.action, "reason": hit.reason}
                )
                self._record(ctx, subject_id, rule.action, rule, hit.reason, hit.details)
        if not triggered:
            self._record(ctx, subject_id, "allow", None, None, {})
            return Verdict("allow", triggered)
        worst = max((t["decision"] for t in triggered), key=lambda d: SEVERITY[d])
        return Verdict(worst, triggered)

    @staticmethod
    def _record(ctx: RuleContext, subject_id, decision, rule: Rule | None, reason, details):
        RiskDecision.objects.create(
            subject_type=ctx.subject,
            subject_id=subject_id,
            wallet=ctx.wallet,
            amount=ctx.amount,
            currency=ctx.wallet.currency,
            decision=decision,
            rule_id=rule.id if rule else None,
            rule_type=rule.type if rule else None,
            reason=reason,
            details=details,
        )


_engine: RiskEngine | None = None


def get_engine() -> RiskEngine:
    global _engine
    if _engine is None:
        _engine = RiskEngine.from_file(settings.RULES_FILE)
    return _engine


def set_engine(engine: RiskEngine | None) -> None:
    """Swap the engine (tests); None reloads from RULES_FILE on next use."""
    global _engine
    _engine = engine
