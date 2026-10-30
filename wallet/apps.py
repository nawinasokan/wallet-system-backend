from django.apps import AppConfig


class WalletConfig(AppConfig):
    name = "wallet"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import schema  # noqa: F401  (registers the OpenAPI extensions)
