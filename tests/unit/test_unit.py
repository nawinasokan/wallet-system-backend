import pytest

from wallet.bank import MockBank
from wallet.money import parse_amount
from wallet.risk.engine import RiskEngine
from wallet.risk.types import Rule, RuleContext, RuleHit
from wallet.services.admin import TRANSITIONS


class TestParseAmount:
    def test_accepts_positive_integers_and_digit_strings(self):
        assert parse_amount(1) == 1
        assert parse_amount("1000000000000000") == 10**15  # the cap, exact
        with pytest.raises(ValueError, match="too large"):
            parse_amount("1000000000000001")

    @pytest.mark.parametrize(
        "v", [0, -1, 1.5, float("nan"), "1.5", "1e3", "-4", "", " 5", "0x10", None, {}, True, 2**60, "99999999999999999"]
    )
    def test_rejects(self, v):
        with pytest.raises((ValueError, TypeError)):
            parse_amount(v)


class Ctx:
    """A RuleContext stand-in; the engine's DB write is stubbed so these run without a database."""

    def __init__(self, subject="transfer"):
        self.subject, self.amount = subject, 10
        self.wallet = type("W", (), {"id": "w1", "currency": "USD"})()


@pytest.fixture
def recorded(monkeypatch):
    rows: list = []
    monkeypatch.setattr(RiskEngine, "_record", staticmethod(lambda ctx, sid, decision, rule, reason, details: rows.append(
        (decision, rule.id if rule else None))))
    return rows


def rule(id_, action, hit, applies_to=("transfer", "withdrawal")):
    return Rule(id_, "stub", action, list(applies_to), lambda ctx: RuleHit(f"{id_} hit") if hit else None)


class TestRiskEngine:
    def test_allows_when_nothing_triggers_and_records_a_single_allow(self, recorded):
        v = RiskEngine([rule("a", "block", False)]).evaluate(Ctx(), "s1")
        assert v.decision == "allow" and recorded == [("allow", None)]

    def test_block_beats_flag_and_every_triggered_rule_is_recorded(self, recorded):
        v = RiskEngine([rule("f", "flag", True), rule("b", "block", True)]).evaluate(Ctx(), "s1")
        assert v.decision == "block"
        assert [t["rule_id"] for t in v.triggered] == ["f", "b"]
        assert recorded == [("flag", "f"), ("block", "b")]

    def test_skips_rules_that_do_not_apply_to_the_subject(self, recorded):
        assert RiskEngine([rule("w", "block", True, ["withdrawal"])]).evaluate(Ctx("transfer"), "s1").decision == "allow"

    def test_is_built_from_config_and_a_pluggable_registry(self, recorded):
        registry = {"always": lambda cfg: Rule(cfg["id"], cfg["type"], cfg["action"], ["transfer"], lambda c: RuleHit("custom"))}
        engine = RiskEngine.from_configs(
            [{"id": "custom-1", "type": "always", "action": "flag", "params": {}},
             {"id": "off", "type": "always", "action": "block", "enabled": False, "params": {}}], registry)
        assert len(engine.rules) == 1
        assert engine.evaluate(Ctx(), "s1").decision == "flag"

    def test_rejects_unknown_types_duplicate_ids_and_bad_params_at_load_time(self):
        vel = {"type": "velocity_outgoing_transfers", "action": "flag"}
        with pytest.raises(ValueError, match="Unknown risk rule type"):
            RiskEngine.from_configs([{"id": "x", "type": "nope", "action": "flag", "params": {}}])
        with pytest.raises(ValueError, match="Duplicate"):
            RiskEngine.from_configs([{"id": "d", **vel, "params": {"maxTransfers": 1, "windowMinutes": 1}}] * 2)
        with pytest.raises(ValueError, match="positive integer"):
            RiskEngine.from_configs([{"id": "v", **vel, "params": {"maxTransfers": 0}}])

    def test_the_shipped_rules_json_loads(self):
        from django.conf import settings

        types = sorted(r.type for r in RiskEngine.from_file(settings.RULES_FILE).rules)
        assert types == ["new_wallet_large_amount", "velocity_outgoing_transfers", "withdrawal_after_large_topup"]


class TestStateMachine:
    def test_transitions(self):
        assert TRANSITIONS["freeze"] == (["active"], "frozen")
        assert TRANSITIONS["unfreeze"] == (["frozen"], "active")
        assert TRANSITIONS["close"][1] == "closed"
        assert all("closed" not in frm for frm, _ in TRANSITIONS.values())  # closed is terminal


class TestMockBank:
    @staticmethod
    def run(suffix):
        sent = []
        res = MockBank(lambda p, d: sent.append(p), 0).submit("w", {"account_number": f"1234{suffix}"})
        return res, sent

    def test_success_failure_timeout_duplicates_and_sync_reject(self):
        assert [p["status"] for p in self.run("5555")[1]] == ["completed"]
        assert [p["status"] for p in self.run("0000")[1]] == ["failed"]
        assert self.run("9999")[1] == []
        dup = self.run("7777")[1]
        assert len(dup) == 3 and dup[0]["event_id"] == dup[1]["event_id"] != dup[2]["event_id"]
        assert self.run("1111")[0].accepted is False
