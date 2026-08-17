# Andruha Identity Service

Identity owns credentials, account authentication state, authentication sessions,
and refresh-token rotation. It does not own editable profiles, messages, media,
realtime delivery, or gateway authorization policy.

The service uses FastAPI with hexagonal boundaries:
`entrypoints -> application -> domain`; infrastructure implements application
ports. PostgreSQL is the durable source of truth. Valkey is an optional hot path
for refresh idempotency and may be unavailable without making refresh unsafe.

## HTTP API

All business routes are under `/api/v1/auth`:

| Method | Path | Result |
|---|---|---|
| `POST` | `/register` | Create an account (`201`) |
| `POST` | `/login` | Set access and refresh cookies (`204`) |
| `POST` | `/refresh` | Rotate cookies idempotently (`204`) |
| `POST` | `/logout` | Revoke the session and delete cookies (`204`) |
| `GET` | `/me` | Resolve the current identity from a trusted bearer token |
| `POST` | `/login/test` | Return tokens for non-production integration tests only |

Refresh requires an `Idempotency-Key` header of 8-128 characters. The same key
and refresh token replays the committed replacement pair. Reusing a key for a
different token returns `409`; an active winner returns `423` with `Retry-After`;
loss of required replay safety returns `503`. Auth responses use stable error
codes and `Cache-Control: no-store`.

Operational endpoints are `GET /health/live`, `GET /health/ready`, and
`GET /metrics`. PostgreSQL failure makes readiness `503`. Valkey failure is
reported as degraded while readiness remains `200` when PostgreSQL is healthy.

## Durable refresh and replay security

Refresh-token consumption, replacement-token creation, session idle extension
or revocation, and insertion of the durable idempotency result commit in one
PostgreSQL unit of work. A unique fence on
`(subject_id, operation, key_hash)` selects the concurrent winner.

Raw idempotency keys and raw tokens are never persisted. The durable and Valkey
success result is an AES-256-GCM envelope with a fresh 96-bit nonce, key ID, and
authenticated context binding the identity, operation, hashes, and result
version. Replay also checks that the referenced replacement refresh token still
exists, is unused, and belongs to an active session. Stale replay returns a safe
authentication failure and clears auth cookies.

## Configuration and keys

See `.env.example` for all non-secret settings. Important groups are:

- `DATABASE_*`, `RUN_MIGRATIONS`;
- `VALKEY_*`, `IDEMPOTENCY_*`;
- `JWT_*`, access/session TTLs, and cookie policy;
- `REPLAY_ENCRYPTION_ACTIVE_KEY_ID` and `REPLAY_ENCRYPTION_KEY_PATHS`.

JWT and replay keys are files, not environment values. For local root Compose,
create ignored files under `C:\Projects\Andruha\.secrets\identity`:

```powershell
New-Item -ItemType Directory -Force C:\Projects\Andruha\.secrets\identity
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out C:\Projects\Andruha\.secrets\identity\jwt-private.pem
openssl rsa -pubout -in C:\Projects\Andruha\.secrets\identity\jwt-private.pem -out C:\Projects\Andruha\.secrets\identity\jwt-public.pem
openssl rand -out C:\Projects\Andruha\.secrets\identity\replay-v1.key 32
```

Startup fails closed when an RSA key is invalid, the replay active key is absent,
or a replay key is not exactly 32 bytes. Rotation is supported by listing old
decrypt-only replay keys in `REPLAY_ENCRYPTION_KEY_PATHS` while selecting one
active encrypt key.

## Database migrations

This greenfield repository has one baseline Alembic revision creating `users`,
`auth_sessions`, `refresh_tokens`, and `idempotency_records`:

```powershell
poetry run alembic upgrade head
poetry run alembic downgrade base
```

The container entrypoint runs `alembic upgrade head` when
`RUN_MIGRATIONS=true`. Retention is controlled by
`IDEMPOTENCY_RESULT_TTL_SECONDS`; repository cleanup is bounded and uses
`SKIP LOCKED`.

## Local verification

Python 3.14 is required.

```powershell
poetry sync --with dev --no-root
poetry run ruff check .
poetry run ruff format --check .
pyright
poetry run pytest tests/unit
poetry run pytest tests/integration
poetry run coverage report --show-missing --fail-under=80
poetry run pip-audit
docker build --target runtime --tag andruha/identity-service:local .
```

The integration suite uses real PostgreSQL and Valkey. It connects to
`IDENTITY_TEST_DATABASE_URL` / `IDENTITY_TEST_VALKEY_URL` when supplied (as in
CI), otherwise it starts disposable Testcontainers automatically. The detailed
scenario map is in `tests/integration/README.md`.

From `C:\Projects\Andruha`, validate and start the routed stack with:

```powershell
docker compose config --quiet
docker compose up -d --build identity-postgres valkey identity-service api-gateway
```

The architecture and threat-model decision is documented in
`docs/identity-idempotency-architecture.md` in the superproject. Cassandra
sessions/idempotency, Kafka/outbox/profile provisioning, JWT session-version
claims, registration idempotency, abuse controls, and session-management APIs
remain explicitly future work.
