from django.db import models
from django.db.models import Func


class ClockTimestamp(Func):
    """Postgres clock_timestamp(): real wall-clock time, not the transaction start time.
    (`now()` is frozen for a whole transaction, which would break the windows the risk rules use.)"""

    function = "clock_timestamp"
    output_field = models.DateTimeField()
    arity = 0
