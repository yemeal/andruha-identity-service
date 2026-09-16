# Andruha Identity Service

Identity owns credentials, account authentication state, authentication sessions,
refresh-token rotation, and reliable publishing of identity lifecycle events (such as user registration) via Transactional Outbox. It does not own editable profiles, messages, media, realtime delivery, or gateway authorization policy.

The service uses FastAPI and FastStream with strict hexagonal boundaries:
`entrypoints -> application -> domain`; infrastructure implements application ports. PostgreSQL is the durable source of truth. Valkey is an optional hot path for refresh idempotency and may be unavailable without making refresh unsafe.

---

## Architecture and Entrypoints

The service provides three independent entrypoint processes built from the same codebase:

1. **HTTP API (`app.entrypoints.http.main:create_app`)**:
   Serves authentication and session management endpoints behind the API Gateway.
2. **Outbox Relay Worker (`app.entrypoints.messaging.relay:main`)**:
   Independent background process polling PostgreSQL outbox table using non-blocking leases (`FOR UPDATE SKIP LOCKED`), publishing integration events to Apache Kafka outside database transactions, handling exponential backoff, dead-letter quarantine, and graceful shutdown on `SIGINT`/`SIGTERM`.
3. **Registration Reconciler (`app.entrypoints.maintenance.registration_reconciler:main`)**:
   Recovers durable registration operations after Profile timeouts, circuit-open responses,
   process crashes, or an ambiguous Identity commit. It uses expiring PostgreSQL leases
   and fencing tokens; every Profile call still goes through `ProfileProvisionerProtocol`.

---

## HTTP API

All business routes are under `/api/v1/auth`:

| Method | Path | Result |
|---|---|---|
| `POST` | `/register` | Confirm profile creation, then commit account and outbox event (`201`); durable recovery pending (`202`) |
| `POST` | `/login` | Set access and refresh HttpOnly cookies (`204`) |
| `POST` | `/refresh` | Rotate cookies idempotently (`204`) |
| `POST` | `/logout` | Revoke the session and delete cookies (`204`) |
| `GET` | `/me` | Resolve current identity from trusted internal Bearer token |
| `POST` | `/login/test` | Return tokens for non-production integration tests only |

Registration and refresh require an `Idempotency-Key` header (8–128 chars).
For registration, `201` means Profile returned `204` and Identity atomically committed
the user, outbox event, and completed operation. `202` means the durable operation is
accepted but there is no registered Identity user yet; retry the same request with the
same key after `Retry-After`. Reusing a key with another email or password returns
`409 Conflict`.

For refresh, reusing a key for the same token replays the committed token pair. Reusing
a key for a different token returns `409 Conflict`; concurrent winner execution returns
`423 Locked` with `Retry-After`; loss of required replay safety returns
`503 Service Unavailable`.

Operational endpoints are `GET /health/live`, `GET /health/ready`, and `GET /metrics`.

---

## Transactional Outbox & Messaging

Registration synchronously calls User Profile through `ProfileProvisionerProtocol`.
The HTTP adapter sends `PUT /internal/v1/profiles/{user_id}` with `registered_at`
and `X-Service-Token`. Only `204` confirms creation of both profile and settings.
Before the call, Identity commits a `RegistrationOperation` containing the reserved
user ID, normalized email, Argon2 hash, idempotency-key hash, and an expiring claim.
No database transaction remains open while Profile is called. After `204`, Identity
atomically inserts the `User`, inserts the outbox event, marks the operation completed,
and scrubs the temporary password hash from the operation.
Transport failures, `423`, `429`, and retryable `5xx` receive one bounded retry
with the same UUID and timestamp. `Retry-After` is honored up to the configured
maximum delay; permanent `4xx` responses such as `409` are not retried. The
Profile CommandBus owns durable provisioning idempotency.

Configure `PROFILE_SERVICE_URL`, `PROFILE_SERVICE_TOKEN` (matching User Profile
`INTERNAL_API_TOKEN`), `PROFILE_SERVICE_TIMEOUT_SECONDS`, the bounded retry
delays, and the circuit-breaker threshold/recovery interval in `core/settings`.
Dishka owns the shared async HTTP client and closes it with the container.
Missing credentials fail closed for registration. Each of at most two synchronous
attempts has the configured HTTP timeout. A shared application-scoped circuit breaker fails fast
after repeated dependency failures and permits one recovery probe after cooldown;
permanent `4xx` responses do not open it.

This is not a distributed transaction: a lost response or failed Identity commit
can temporarily leave a profile without an account. The durable operation and
registration reconciler converge that state by repeating the idempotent Profile PUT
and finalizing Identity. GET requests never repair data. Exhausted or permanently
rejected operations move to `BLOCKED` for explicit operational redrive. Existing
accounts predating this contract still need a separate backfill before claiming the
invariant for all users.

After correcting the underlying cause, redrive one blocked operation from the
reconciler container:

```bash
python -m app.entrypoints.maintenance.registration_reconciler --redrive <operation-uuid>
```

Kafka remains independent of registration success. The outbox event notifies
other consumers; profile creation no longer waits for event delivery.

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
* `REGISTRATION_*` (reconciler polling, claim lease, backoff, attempts, shutdown timeout)
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
poetry run ty check --error-on-warning
poetry run pytest tests/unit
poetry run pytest tests/integration     # PostgreSQL + Valkey + Profile peer
poetry run pip-audit
docker build --target runtime --tag andruha/identity-service:local .
```

To run the entire local stack (API Gateway, Identity Service, Identity Relay, PostgreSQL, Valkey, Kafka) from root:

```powershell
docker compose config --quiet
docker compose up -d --build identity-postgres valkey kafka user-profile-service identity-service identity-relay identity-registration-reconciler api-gateway
```
