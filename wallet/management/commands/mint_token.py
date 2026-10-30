from django.core.management.base import BaseCommand

from wallet.auth import Actor, sign_token


class Command(BaseCommand):
    help = "Mint a JWT: manage.py mint_token alice [user|admin]"

    def add_arguments(self, parser):
        parser.add_argument("sub")
        parser.add_argument("role", nargs="?", default="user", choices=["user", "admin"])

    def handle(self, *args, **o):
        self.stdout.write(sign_token(Actor(o["sub"], o["role"])))
