from dataclasses import dataclass, field
from typing import Callable

from ..models import Wallet


@dataclass
class RuleContext:
    wallet: Wallet  # the sending wallet, already row-locked, on the caller's open transaction
    subject: str  # 'transfer' | 'withdrawal'
    amount: int


@dataclass
class RuleHit:
    reason: str
    details: dict = field(default_factory=dict)


@dataclass
class Rule:
    """A rule only answers "did I trigger?". The configured action (flag/block) is applied by the engine."""

    id: str
    type: str
    action: str  # 'flag' | 'block'
    applies_to: list[str]
    evaluate: Callable[[RuleContext], RuleHit | None]


RuleFactory = Callable[[dict], Rule]
