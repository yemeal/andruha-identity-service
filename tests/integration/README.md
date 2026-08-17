# Identity integration tests

These tests exercise real application paths rather than mocked repositories:

```text
HTTP -> FastAPI -> application -> PostgreSQL/Valkey -> Argon2/RS256/AES-GCM
```

PostgreSQL and Valkey are always real. When
`IDENTITY_TEST_DATABASE_URL` and `IDENTITY_TEST_VALKEY_URL` are set, the suite
uses those Docker/CI services. Otherwise session-scoped Testcontainers start
disposable PostgreSQL 18 and Valkey 8.1 containers. Database rows and Valkey
keys are cleared between stateful scenarios.

## Coverage map

| Area | Executable coverage |
|---|---|
| Register | persistence, normalization, Argon2 hash, duplicate conflict, concurrent unique race |
| Login | real Argon2 verification, identical public failures, disabled account, session/token creation, multiple devices, JWT claims |
| Access token | valid round trip, expiry boundary, future `iat`/`nbf`, malformed token, wrong scheme/type/key/signature, cross-user isolation |
| Refresh | rotation chain, expired/revoked/unknown token, family replay revocation, exact idempotent retry, key conflict, same/different-key races |
| Logout | idempotent repeat, refresh rejection, per-device isolation, logout/refresh and logout/logout races |
| Valkey | Lua acquire/replay/conflict, owner CAS, renew/abandon, processing/result TTL, crash recovery, corruption, 100-client lock race |
| PostgreSQL | baseline schema, required/unique/FK/check constraints, atomic rollback, durable fence race, conflict, retention cleanup |
| Security | no password/hash in API, no plaintext token pair or raw idempotency key in PostgreSQL/Valkey, fresh AES-GCM nonces |
| Failures | PostgreSQL-down safe 5xx/readiness, Valkey-down durable fallback, corrupted and stale replay fail closed |

Kafka/outbox registration publishing, `logout-all`, session listing/revocation,
and Cassandra session persistence are not production capabilities in this
migration. The scope-boundary tests assert that they are not accidentally
introduced. Positive tests for those flows belong in the feature branch that
adds their contracts, schema, adapters, and runtime behavior; this suite does
not hide them behind `skip` or `xfail`.

## Run

```powershell
poetry run pytest tests/integration
poetry run pytest tests/integration -m race
```

Docker must be available when external test URLs are not supplied.
