import pytest
from django.db import connections
from django.test.utils import setup_databases, teardown_databases


@pytest.fixture(scope="session")
def db_ready(django_db_blocker):
    """One throwaway `test_<name>` database for the whole run, created from the migrations and dropped at the end.

    Real commits across threads are needed to test concurrency, and the ledger tables forbid
    TRUNCATE/DELETE (so pytest-django's flush can't be used); tests namespace their data with
    unique owner ids instead of being rolled back."""
    with django_db_blocker.unblock():
        config = setup_databases(verbosity=0, interactive=False)
        try:
            yield
        finally:
            connections.close_all()
            teardown_databases(config, verbosity=0)
