# Andruha Identity Service

Identity owns credentials, account authentication state, authentication sessions,
refresh-token rotation, and reliable publishing of identity lifecycle events (such as user registration) via Transactional Outbox. It does not own editable profiles, messages, media, realtime delivery, or gateway authorization policy.

The service uses FastAPI and FastStream with strict hexagonal boundaries:
`entrypoints -> application -> domain`; infrastructure implements application ports. PostgreSQL is the durable source of truth. Valkey is an optional hot path for refresh idempotency and may be unavailable without making refresh unsafe.

---

## Architecture and Entrypoints

The service provides two independent entrypoint processes built from the same codebase:

1. **HTTP API (`app.entrypoints.http.main:create_app`)**:
   Serves authentication and session management endpoints behind the API Gateway.
2. **Outbox Relay Worker (`app.entrypoints.messaging.relay:main`)**:
   Independent background process polling PostgreSQL outbox table using non-blocking leases (`FOR UPDATE SKIP LOCKED`), publishing integration events to Apache Kafka outside database transactions, handling exponential backoff, dead-letter quarantine, and graceful shutdown on `SIGINT`/`SIGTERM`.

---

## HTTP API

All business routes are under `/api/v1/auth`:

| Method | Path | Result |
|---|---|---|
| `POST` | `/register` | Create an account and atomically persist outbox event (`201`) |
| `POST` | `/login` | Set access and refresh HttpOnly cookies (`204`) |
| `POST` | `/refresh` | Rotate cookies idempotently (`204`) |
| `POST` | `/logout` | Revoke the session and delete cookies (`204`) |
| `GET` | `/me` | Resolve current identity from trusted internal Bearer token |
| `POST` | `/login/test` | Return tokens for non-production integration tests only |

Refresh requires an `Idempotency-Key` header (8–128 chars). Reusing a key for the same token replays the committed token pair. Reusing a key for a different token returns `409 Conflict`; concurrent winner execution returns `423 Locked` with `Retry-After`; loss of required replay safety returns `503 Service Unavailable`.

Operational endpoints are `GET /health/live`, `GET /health/ready`, and `GET /metrics`.

---

## Transactional Outbox & Messaging

User registration publishes integration event `identity.user_registered.v1` (defined in root contracts `contracts/identity/events/user-registered.v1.schema.json`):

* **Atomic Dual-Write**: The new `User` record and the `OutboxMessage` row are inserted inside the exact same PostgreSQL database transaction.
* **Lease-based Concurrency**: The relay worker claims batches using atomic CTE leases with `FOR UPDATE SKIP LOCKED`.
* **Partitioned Concurrency with Strict FIFO**: Messages are grouped by partition key (`user_id`). Disjoint keys publish concurrently via `asyncio.gather`, while messages with identical keys execute in strict FIFO sequence.
* **Transient & Permanent Failure Handling**:
  * Network timeouts / Kafka broker disconnections trigger exponential backoff with full jitter and reschedule `available_at`.
  * Malformed payloads or schema violations are immediately moved to `QUARANTINED` status without blocking healthy partitions.
* **Lease Recovery**: If a relay worker crashes midway, its expired lease is safely recovered by another worker once `claim_expires_at` passes.

---

## Durable Refresh and Replay Security

Refresh-token consumption, replacement-token creation, session idle extension or revocation, and insertion of the durable idempotency result commit in one PostgreSQL unit of work. A unique fence on `(subject_id, operation, key_hash)` selects the concurrent winner.

Raw idempotency keys and raw tokens are never persisted. The durable and Valkey success result is an AES-256-GCM envelope with a fresh 96-bit nonce, key ID, and authenticated context binding identity, operation, hashes, and result version. Stale replay returns a safe authentication failure and clears auth cookies.

---

## Configuration and Keys

Configuration is loaded into strongly-typed Pydantic settings models (`AppSettings`, `PostgresSettings`, `ValkeySettings`, `KafkaSettings`, `OutboxSettings`, `SecuritySettings`, `LoggingSettings`).

See `.env.example` for non-secret configuration. Key settings include:

* `DATABASE_*`, `RUN_MIGRATIONS`
* `VALKEY_*`, `IDEMPOTENCY_*`
* `KAFKA_*` (bootstrap servers, client ID, acks)
* `OUTBOX_*` (poll interval, batch size, claim lease, backoff multiplier/jitter, shutdown timeout)
* `JWT_*` (issuer, audiences, key paths, TTLs, clock skew)
* `REPLAY_ENCRYPTION_ACTIVE_KEY_ID` and `REPLAY_ENCRYPTION_KEY_PATHS`

JWT RSA keys and AES-GCM replay keys are read from secret files:

```powershell
New-Item -ItemType Directory -Force C:\Projects\Andruha\.secrets\identity
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out C:\Projects\Andruha\.secrets\identity\jwt-private.pem
openssl rsa -pubout -in C:\Projects\Andruha\.secrets\identity\jwt-private.pem -out C:\Projects\Andruha\.secrets\identity\jwt-public.pem
openssl rand -out C:\Projects\Andruha\.secrets\identity\replay-v1.key 32
```

---

## Database Migrations

Alembic manages all schema migrations for the durable PostgreSQL store:

* `users`: Credentials, salt, roles, registration timestamp.
* `auth_sessions`: Active and revoked authentication sessions.
* `refresh_tokens`: Rotated cryptographically hashed tokens.
* `idempotency_records`: Encrypted replay results and concurrency fences.
* `outbox`: Transactional outbox buffer with dispatch indexes and lifecycle constraints.

```powershell
poetry run alembic upgrade head
poetry run alembic downgrade base
```

---

## Verification & Testing

The service is fully covered with both unit and end-to-end integration tests against real PostgreSQL and Valkey Testcontainers:

```powershell
poetry sync --with dev --no-root
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest tests/unit            # 274 unit tests
poetry run pytest tests/integration     # 100 integration tests (Postgres + Valkey)
poetry run pip-audit
docker build --target runtime --tag andruha/identity-service:local .
```

To run the entire local stack (API Gateway, Identity Service, Identity Relay, PostgreSQL, Valkey, Kafka) from root:

```powershell
docker compose config --quiet
docker compose up -d --build identity-postgres valkey kafka identity-service identity-relay api-gateway
```
