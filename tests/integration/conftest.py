import pytest

from tests.helpers import Bank
from wallet.risk.engine import RiskEngine, set_engine


@pytest.fixture(autouse=True)
def _db(db_ready):
    yield


@pytest.fixture(autouse=True)
def _risk_engine():
    """Default: no risk rules. risk tests override this fixture with their own rules."""
    set_engine(RiskEngine([]))
    yield
    set_engine(None)


@pytest.fixture
def bank():
    return Bank()
