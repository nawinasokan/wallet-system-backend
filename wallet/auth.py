import hmac
import time
from dataclasses import dataclass

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication

from .errors import ApiError


@dataclass(frozen=True)
class Actor:
    id: str
    role: str  # 'user' | 'admin'
    is_authenticated: bool = True

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def sign_token(actor: Actor, expires_in: int = 3600) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": actor.id, "role": actor.role, "iat": now, "exp": now + expires_in},
        settings.JWT_SECRET,
        algorithm="HS256",
    )


class WalletAuthentication(BaseAuthentication):
    """Resolves the caller from `X-API-Key` or `Authorization: Bearer <jwt>`. 401 otherwise."""

    def authenticate_header(self, request):
        return 'Bearer realm="wallet"'

    def authenticate(self, request):
        api_key = request.headers.get("X-API-Key")
        if api_key is not None:
            entry = next(
                (v for k, v in settings.API_KEYS.items() if hmac.compare_digest(k, api_key)), None
            )
            if entry is None:
                raise ApiError(401, "unauthorized", "Invalid API key")
            return Actor(entry["sub"], entry["role"]), None
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            raise ApiError(401, "unauthorized", "Missing credentials")
        try:
            claims = jwt.decode(header[7:], settings.JWT_SECRET, algorithms=["HS256"])
            sub, role = claims.get("sub"), claims.get("role")
            if not sub or role not in ("user", "admin"):
                raise ValueError("bad claims")
        except Exception:
            raise ApiError(401, "unauthorized", "Invalid or expired token") from None
        return Actor(sub, role), None


def require_admin(actor: Actor) -> Actor:
    if not actor.is_admin:
        raise ApiError(403, "forbidden", "Admin role required")
    return actor


def assert_can_access(actor: Actor, wallet) -> None:
    """Users may touch only their own wallets; admins may touch all."""
    if not actor.is_admin and wallet.owner_id != actor.id:
        raise ApiError(403, "forbidden", "You do not have access to this wallet")
