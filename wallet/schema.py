"""OpenAPI hints so Swagger describes the custom auth class and the minor-unit amount type."""
from drf_spectacular.extensions import OpenApiAuthenticationExtension, OpenApiSerializerFieldExtension


class WalletAuthScheme(OpenApiAuthenticationExtension):
    target_class = "wallet.auth.WalletAuthentication"
    name = "bearerAuth"

    def get_security_definition(self, auto_schema):
        return {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}


class AmountFieldScheme(OpenApiSerializerFieldExtension):
    target_class = "wallet.serializers.AmountField"
    match_subclasses = True

    def map_serializer_field(self, auto_schema, direction):
        return {
            "description": "Positive integer in minor units (e.g. cents). Integer or digit string; never a decimal.",
            "oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": "^[0-9]{1,16}$"}],
        }
