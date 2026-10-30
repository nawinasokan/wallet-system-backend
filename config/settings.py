"""Settings. Everything operational is an environment variable (see README)."""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _parse_api_keys(raw: str) -> dict[str, dict[str, str]]:
    """API_KEYS="key1:user-1:user,key2:ops:admin" -> {key: {sub, role}}"""
    out: dict[str, dict[str, str]] = {}
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        bits = part.split(":")
        if len(bits) != 3 or not bits[0] or not bits[1] or bits[2] not in ("user", "admin"):
            raise ValueError(f'Invalid API_KEYS entry "{part}", expected key:subject:user|admin')
        out[bits[0]] = {"sub": bits[1], "role": bits[2]}
    return out


def _database(url: str) -> dict:
    u = urlparse(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": u.path.lstrip("/"),
        "USER": unquote(u.username or ""),
        "PASSWORD": unquote(u.password or ""),
        "HOST": u.hostname or "localhost",
        "PORT": str(u.port or 5432),
        "CONN_MAX_AGE": int(_env("DB_CONN_MAX_AGE", "60")),
    }


SECRET_KEY = _env("DJANGO_SECRET_KEY", "dev-only-django-secret-change-me")
DEBUG = _env("DJANGO_DEBUG", "false") == "true"
ALLOWED_HOSTS = _env("ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "wallet",
]

MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    }
]

DATABASES = {"default": _database(_env("DATABASE_URL", "postgres://wallet:wallet@localhost:5435/wallet"))}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
STATIC_URL = "static/"
APPEND_SLASH = False

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["wallet.auth.WalletAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": [],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "EXCEPTION_HANDLER": "wallet.exceptions.api_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Wallet Service API",
    "VERSION": "1.0.0",
    "DESCRIPTION": (
        "Wallets, top-ups, transfers and withdrawals over an immutable double-entry ledger.\n\n"
        "All amounts are **integers in minor units** and are returned as **strings**.\n"
        "Authenticate with `Authorization: Bearer <JWT>` (claims `sub`, `role`) or `X-API-Key`."
    ),
    "SERVE_INCLUDE_SCHEMA": False,
    "SERVE_AUTHENTICATION": [],  # the docs are public; the API itself is not
    "COMPONENT_SPLIT_REQUEST": True,
    "SECURITY": [{"bearerAuth": []}, {"apiKey": []}],
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
            "apiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"},
        }
    },
}

# --- wallet service ---------------------------------------------------------------------------
JWT_SECRET = _env("JWT_SECRET", "dev-only-change-me")
API_KEYS = _parse_api_keys(_env("API_KEYS", ""))
ENABLE_DEV_TOKENS = _env("ENABLE_DEV_TOKENS", "false") == "true"
# minor units, for wallets without their own limit
DEFAULT_DAILY_LIMIT = int(_env("DEFAULT_DAILY_LIMIT", "1000000000"))
RULES_FILE = _env("RULES_FILE", str(BASE_DIR / "rules" / "rules.json"))
BANK_WEBHOOK_SECRET = _env("BANK_WEBHOOK_SECRET", "dev-bank-secret")
# a withdrawal stuck in `processing` longer than this is failed and refunded
BANK_TIMEOUT_MS = int(_env("BANK_TIMEOUT_MS", "60000"))
BANK_CALLBACK_URL = _env("BANK_CALLBACK_URL", "http://127.0.0.1:8000/bank/callbacks")
MOCK_BANK_DELAY_MS = int(_env("MOCK_BANK_DELAY_MS", "1500"))
# when false the mock bank stays silent (tests drive callbacks by hand)
MOCK_BANK_ENABLED = _env("MOCK_BANK_ENABLED", "true") != "false"
WORKER_INTERVAL_MS = int(_env("WORKER_INTERVAL_MS", "1000"))

# --- celery (withdrawal worker) ---------------------------------------------------------------
CELERY_BROKER_URL = _env("CELERY_BROKER_URL", "redis://localhost:6380/0")
CELERY_TASK_ALWAYS_EAGER = False
CELERY_BEAT_SCHEDULE = {
    "process-withdrawals": {
        "task": "wallet.tasks.process_withdrawals",
        "schedule": WORKER_INTERVAL_MS / 1000,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
