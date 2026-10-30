import time

from django.conf import settings
from django.core.management.base import BaseCommand

from wallet.bank import MockBank, thread_callback_sender
from wallet.tasks import run_worker_once


class Command(BaseCommand):
    help = "Standalone withdrawal worker loop (no Celery/Redis needed). Prefer Celery beat in production."

    def handle(self, *args, **opts):
        bank = None
        if settings.MOCK_BANK_ENABLED:
            bank = MockBank(
                thread_callback_sender(settings.BANK_CALLBACK_URL, settings.BANK_WEBHOOK_SECRET),
                settings.MOCK_BANK_DELAY_MS,
            )
        self.stdout.write("withdrawal worker started")
        while True:
            try:
                run_worker_once(bank)
            except Exception as e:  # keep ticking
                self.stderr.write(f"worker tick failed: {e}")
            time.sleep(settings.WORKER_INTERVAL_MS / 1000)
