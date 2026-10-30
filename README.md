# Wallet Service

Wallets that hold money, accept top-ups, transfer between users and withdraw to a bank, on top of an
**immutable double-entry ledger**, with a pluggable fraud/velocity rule engine and wallet lifecycle states.

**Stack:** Python 3.12, Django 6 + Django REST Framework, PostgreSQL 17, Celery + Redis, Docker, pytest.
Money is **`BIGINT` minor units end to end** (DB column → Python `int` → JSON string). No floats anywhere; the API
rejects `1.5`, `"1.50"`, `1e3`.

## Run it

```bash
docker compose up --build          # Postgres + Redis + API + Celery worker + Celery beat on http://localhost:8000
open http://localhost:8000/docs    # Swagger UI (OpenAPI JSON at /docs/json)
```

Docker Compose enables a **dev-only** token endpoint and two demo API keys so Swagger is usable immediately:

```bash
curl -s -XPOST localhost:8000/auth/dev-token -H 'content-type: application/json' -d '{"sub":"alice"}'
curl -s -XPOST localhost:8000/auth/dev-token -H 'content-type: application/json' -d '{"sub":"ops","role":"admin"}'
# or API keys:  X-API-Key: demo-user-key (user-1)   X-API-Key: demo-admin-key (admin)
```

Without Docker for the API (DB + Redis only in Docker):

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
docker compose up -d db redis                  # Postgres on :5435, Redis on :6380
python manage.py migrate
ENABLE_DEV_TOKENS=true python manage.py runserver
celery -A config worker -l info &              # runs the withdrawal tasks
celery -A config beat -l info &                # schedules them every WORKER_INTERVAL_MS
python manage.py mint_token alice [user|admin] # mint a JWT from the CLI
python manage.py run_worker                    # alternative to Celery: a plain polling loop, no Redis needed
```

### Tests

```bash
docker compose up -d db            # the test run creates and drops its own throwaway test database
pytest                             # 97 tests, ~18 s
pytest tests/unit                  # no database needed
```

### Configuration (env)

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL` | `postgres://wallet:wallet@localhost:5435/wallet` | |
| `CELERY_BROKER_URL` | `redis://localhost:6380/0` | |
| `JWT_SECRET` | `dev-only-change-me` | HS256 secret. **Change in production.** |
| `DJANGO_SECRET_KEY` / `DJANGO_DEBUG` | dev value / `false` | |
| `API_KEYS` | – | `key:subject:user\|admin,...` |
| `ENABLE_DEV_TOKENS` | `false` | exposes `POST /auth/dev-token`. **Never in production.** |
| `DEFAULT_DAILY_LIMIT` | `1000000000` | minor units, for wallets without their own limit |
| `RULES_FILE` | `rules/rules.json` | risk rules |
| `BANK_WEBHOOK_SECRET` | `dev-bank-secret` | HMAC key for bank callbacks |
| `BANK_TIMEOUT_MS` | `60000` | `processing` longer than this ⇒ failed + refunded |
| `BANK_CALLBACK_URL` | `http://127.0.0.1:8000/bank/callbacks` | where the mock bank posts |
| `MOCK_BANK_DELAY_MS` / `WORKER_INTERVAL_MS` | `1500` / `1000` | mock bank latency / worker tick |

## API

Auth: `Authorization: Bearer <JWT with sub, role>` or `X-API-Key`. Users touch only their own wallets; admins everything.
Amounts: integer minor units in, **strings** out. Mutating money endpoints **require** `Idempotency-Key`.
Errors: `{"error": {"code", "message", "details?"}}`.

| Method & path | Purpose |
|---|---|
| `GET /wallets` | my wallets (admins: all wallets) |
| `POST /wallets` | create (one per user+currency; admins may pass `owner_id`) |
| `GET /wallets/{id}` | balance, status, limits |
| `POST /wallets/{id}/topups` | mock payment provider (`tok_decline`, `tok_insufficient` fail; anything else succeeds) |
| `POST /transfers` | atomic transfer between two wallets |
| `GET /wallets/{id}/transactions` | statement: `from`, `to` (date-time), `limit`, `cursor` (keyset, newest first) |
| `POST /wallets/{id}/withdrawals` · `GET /withdrawals/{id}` | withdraw to a mock bank account → **202** |
| `POST /bank/callbacks` | HMAC-signed bank webhook (`X-Bank-Signature`) |
| `POST /admin/wallets/{id}/freeze\|unfreeze\|close` | `{reason}` required; audited |
| `GET /admin/wallets/{id}/state-history` | audit trail of state changes |
| `PUT /admin/wallets/{id}/daily-limit` | per-wallet limit (`null` = default) |
| `POST /admin/transfers/{id}/reverse` | compensating entries |
| `GET /admin/flags?decision=flag\|block\|all` | risk decisions with triggering rule |
| `GET /admin/reconciliation` | ledger vs balances check |

Status codes: `201` created · `202` withdrawal accepted · `400` validation · `401/403` auth · `402` payment declined ·
`403 blocked_by_risk_rule` · `404` · `409` state conflicts (`wallet_frozen`, `wallet_closed`, `already_reversed`, …) ·
`422` business rule (`insufficient_funds`, `daily_limit_exceeded`, `idempotency_key_reuse`, …).

**Mock bank** – outcome chosen by the account-number suffix: `…0000` fails asynchronously · `…9999` never calls back
(timeout) · `…7777` sends duplicate + redelivered success callbacks · `…1111` rejected synchronously · otherwise succeeds.
Celery beat runs `process_withdrawals` every tick; the mock bank's callbacks are delivered by a delayed Celery task that
POSTs a real signed request to the API.

## Schema (Django models in `wallet/models.py`, triggers in `wallet/migrations/0002_integrity.py`)

```
wallets(id, owner_id, currency, kind user|system, balance, status, status_reason, daily_limit, created_at)
        UNIQUE(owner_id,currency)  CHECK(kind='system' OR balance>=0)
transactions(id, type, amount, currency, source_wallet_id, dest_wallet_id, reverses_id UNIQUE, reason, metadata, created_at)   -- append-only
ledger_entries(id, transaction_id, wallet_id, direction debit|credit, amount, balance_after, created_at)                      -- append-only
withdrawals(id, wallet_id, amount, destination, status requested|processing|completed|failed, hold_txn_id, resolution_txn_id, …)
bank_callbacks(event_id PK, withdrawal_id, payload, outcome)
idempotency_keys(principal, key, request_hash, status_code, response)   UNIQUE(principal,key)
risk_decisions(subject_type, subject_id, wallet_id, amount, decision allow|flag|block, rule_id, reason, details) -- append-only
wallet_state_history(wallet_id, from_status, to_status, reason, actor, created_at)                                -- append-only
```

Ledger accounts: each user wallet, plus per-currency **system** accounts `system:external` (the outside world: top-ups
in, settled withdrawals out) and `system:withdrawal_hold` (funds reserved for in-flight withdrawals).

| Movement | Debit → Credit |
|---|---|
| top-up | external → wallet |
| transfer | source → destination |
| withdrawal request | wallet → hold |
| withdrawal completed | hold → external |
| withdrawal failed (refund) | hold → wallet (`reverses_id` = the hold) |
| transfer reversal | destination → source (`reverses_id` = the transfer) |

**Invariants enforced by the database itself** (not just app code):
- `transactions`, `ledger_entries`, `risk_decisions`, `wallet_state_history` reject `UPDATE`, `DELETE`, `TRUNCATE` (triggers).
- A deferred constraint trigger checks **debits = credits per transaction at commit**.
- `CHECK (balance >= 0)` on user wallets.
- `transactions.reverses_id UNIQUE`: a transfer is reversed at most once, a withdrawal refunded at most once.
- `UNIQUE` index on settle-per-withdrawal.

Balance of a wallet = `SUM(credits) − SUM(debits)` over its entries; `wallets.balance` is a locked, transactionally-updated cache of that,
and `ledger_entries.balance_after` is the running balance. `GET /admin/reconciliation` and the test-suite verify they agree, that the whole
ledger sums to zero, and that the hold account equals the sum of pending withdrawals.

## Design decisions and trade-offs

**Concurrency: pessimistic row locks, ordered.** Every money operation runs in `transaction.atomic()` and starts with
`Wallet.objects.select_for_update().filter(id__in=…).order_by("id")`. One lock-acquisition order (ascending id) means A→B and B→A cannot
form a cycle. After the lock, status, balance, daily total and risk counts are read in the same transaction, so they cannot change underneath.
100 parallel transfers from one wallet simply serialise on that row: exactly `balance/amount` succeed. *Trade-off:* throughput per
*wallet* is serial (fine: a wallet's own operations must be ordered anyway); different wallets run in parallel. I chose this over
`SERIALIZABLE` + retries (retry storms under a hot wallet) and over a single `UPDATE … WHERE balance >= x` (which can't also guard status, limits and rule counts).

**Idempotency inside the money transaction.** The key row is inserted in the *same* DB transaction as the movement. A concurrent duplicate
blocks on the unique index, then replays the stored response (`Idempotent-Replayed: true`); a crash leaves neither or both. Keys are scoped to the caller;
the same key with a different body → `422 idempotency_key_reuse`. Business rejections (insufficient funds, blocked…) are stored as well, so a retry gets the same answer;
use a new key to re-attempt. Validation/auth errors aren't stored. Business rejections are *returned* (a `Result`), not raised, so side effects such as risk decisions still commit;
`ApiError` is raised only for failures that must roll everything back.

**Race-safe state.** Freeze/close take the same wallet row lock as transfers, so a freeze either commits before an in-flight transfer reads `status`
(it is rejected) or after it has fully committed. Close also checks `balance = 0` and no pending withdrawals under that lock. Tested with a freeze racing 60 transfers.

**Withdrawals as a state machine with exactly-once refund.** Request: reserve (wallet→hold) + insert row, one transaction → `requested`. The Celery task
claims with `FOR UPDATE SKIP LOCKED` → `processing` and calls the bank. Bank callback (or timeout sweep) locks the withdrawal row and only `processing` may transition.
Redelivered `event_id`s are no-ops (PK); a second event with the same outcome is ignored; a contradicting one is recorded as `conflict` and never applied.
The unique `reverses_id` is a last-resort DB guard against double refunds. Refunds ignore wallet status (a frozen wallet may get its own money back).
*Trade-off:* **timeout ⇒ fail + refund** is what the spec asks for, but a bank that was merely slow and later reports success means money both refunded and paid.
That late success is recorded (`bank_callbacks.outcome = 'conflict'`) rather than silently applied, and needs manual reconciliation. A real system would query the bank's
status API before refunding, or park the item in a `needs_review` state.

**Risk engine: pluggable and race-safe.** `Rule` = `{id, type, action, applies_to, evaluate(ctx) → hit | None}`. Rule *types* live in `wallet/risk/rules/` behind a registry; rule *instances* and thresholds
live in `rules/rules.json` (a restart applies changes). Adding a rule = one file + one registry line; `transfers.py`/`withdrawals.py` call `engine.evaluate()` and know nothing about individual rules.
Strongest decision wins (block > flag > allow); every triggered rule is persisted with its reason, plus a plain `allow` row when nothing fires.
Counting rules (velocity) run **after the sender's wallet lock is taken**, so ten parallel requests from one wallet are evaluated one after another and see each other's committed transfers: exactly N get through (tested).
*Trade-off:* the rules are shipped for the *sender's* wallet; a rule that aggregates across wallets (e.g. per device) would need its own lock/advisory-lock key.
Blocked attempts are logged but don't count toward the velocity window. Rules use `clock_timestamp()` (not `now()`, which is frozen per transaction) for their time windows.

**Reversals.** An admin reversal posts `destination → source`; nothing is edited. If the destination has already spent the money the reversal fails with `409 insufficient_funds_to_reverse`
instead of driving a balance negative (a negative-balance/debt model is a business decision I did not want to invent). Reversals bypass frozen status, limits and rules (admin action), and free the original's daily-limit usage.

**Daily limit** = outgoing transfers + withdrawals since UTC midnight, excluding those that were reversed/refunded; per-wallet override or `DEFAULT_DAILY_LIMIT`. Computed from the ledger under the wallet lock (no counter to drift).
*Trade-off:* a `SUM` per transfer; fine with the `(source_wallet_id, type, created_at)` index, a counter table would be the next step at scale.

**System wallets have no maintained `balance`.** Otherwise every withdrawal and top-up would update (and lock) one global hot row. Their balances are derived from the ledger.

**Top-up** calls the in-process mock provider while holding the wallet lock so the "not closed" check and the credit are atomic. A real, slow provider would use a pending state + webhook, like withdrawals.

**Auth.** HS256 JWT (`sub`, `role`) and static API keys, as a DRF authentication class, with ownership checks in the service layer (403 for other users' wallets). Out of scope: user registration, key rotation, JWKS.

**Why raw SQL in a few places inside a Django app.** The ORM does the CRUD and row locking. Raw SQL is used where the database semantics matter and the ORM hides them:
`INSERT … ON CONFLICT DO NOTHING RETURNING` for the idempotency claim, `UPDATE … RETURNING balance`, `clock_timestamp()` windows, and the reconciliation aggregates.

### Known limitations
- Single currency per wallet; no FX. Transfers across currencies are rejected.
- Rule config is loaded at start-up (no hot reload / admin CRUD for rules).
- Statement `from/to` filter on entry time; `limit ≤ 200`. No export.
- No rate limiting, request signing for clients, or PII encryption of bank details.
- The test database is shared by a whole run (the ledger forbids `TRUNCATE`/`DELETE`, and concurrency tests need real commits), so tests namespace their data with unique owner ids instead of being rolled back.

## Layout

```
config/               Django settings, urls, Celery app
wallet/models.py      schema                     wallet/migrations/   0001 tables · 0002 DB-level integrity triggers
wallet/services/      ledger, transfers, topups, withdrawals, admin, statement, reconciliation
wallet/risk/          engine, rule registry, rules/        wallet/idempotency.py   exactly-once wrapper
wallet/views.py       DRF views + OpenAPI hints            wallet/tasks.py         Celery: withdrawal worker, callback delivery
wallet/bank.py        mock bank + signed webhook sender    wallet/payments.py      mock payment provider
rules/rules.json      risk rule configuration              tests/                  unit · integration · concurrency · reconciliation
```

## AI usage

Some areas Built with Antigravity (Claude Sonnet).