"""Celery tasks: the withdrawal worker (beat schedule in settings) and mock-bank callback delivery."""
import logging

from celery import shared_task
from django.conf import settings

from .bank import MockBank, celery_callback_sender, post_signed_callback
from .services.withdrawals import submit_pending, sweep_timeouts

log = logging.getLogger(__name__)


def run_worker_once(bank: MockBank | None) -> dict:
    submitted = submit_pending(bank) if bank else 0
    timed_out = sweep_timeouts()
    return {"submitted": submitted, "timed_out": timed_out}


@shared_task(name="wallet.tasks.process_withdrawals")
def process_withdrawals() -> dict:
    """Submit `requested` withdrawals to the bank, then fail+refund the ones the bank never answered.
    Safe to run on several workers at once: claims use FOR UPDATE SKIP LOCKED."""
    bank = MockBank(celery_callback_sender(), settings.MOCK_BANK_DELAY_MS) if settings.MOCK_BANK_ENABLED else None
    return run_worker_once(bank)


@shared_task(name="wallet.tasks.deliver_bank_callback", autoretry_for=(Exception,), retry_backoff=True, max_retries=5)
def deliver_bank_callback(payload: dict) -> None:
    post_signed_callback(settings.BANK_CALLBACK_URL, settings.BANK_WEBHOOK_SECRET, payload)
