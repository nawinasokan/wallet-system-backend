import hashlib
import json

from django.db import connection, transaction

from .errors import Result, reject


def hash_request(method: str, path: str, body) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{method}\n{path}\n{canonical}".encode()).hexdigest()


def with_idempotency(principal: str, key: str, request_hash: str, work) -> tuple[Result, bool]:
    """Exactly-once execution per (principal, Idempotency-Key). Returns (result, replayed).

    The key row is claimed INSIDE the same DB transaction as the money movement:
      * the first caller inserts the row, does the work, stores the response, commits (atomic);
      * a concurrent duplicate blocks on the unique index until the first commits, then replays
        the stored response; if the first rolled back, the duplicate claims the key and runs.
    So a crash can never leave "money moved but key unrecorded" or the reverse.
    """
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute(
                """INSERT INTO idempotency_keys (principal, key, request_hash) VALUES (%s, %s, %s)
                   ON CONFLICT (principal, key) DO NOTHING RETURNING id""",
                [principal, key, request_hash],
            )
            claimed = cur.fetchone()
            if claimed is None:
                cur.execute(
                    "SELECT request_hash, status_code, response FROM idempotency_keys"
                    " WHERE principal = %s AND key = %s",
                    [principal, key],
                )
                stored_hash, status_code, response = cur.fetchone()
                if stored_hash != request_hash:
                    return (
                        reject(
                            422,
                            "idempotency_key_reuse",
                            "Idempotency-Key was already used with a different request",
                        ),
                        False,
                    )
                # Django configures psycopg to hand jsonb back as text
                if isinstance(response, str):
                    response = json.loads(response)
                return Result(status_code, response), True
        result = work()
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE idempotency_keys SET status_code = %s, response = %s::jsonb"
                " WHERE principal = %s AND key = %s",
                [result.status, json.dumps(result.body), principal, key],
            )
        return result, False
