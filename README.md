# Andruha Identity Service

## Purpose and current status

This repository is the skeleton for the Andruha Messenger Identity Service. It contains package boundaries and operational HTTP infrastructure only. No messenger business behavior is implemented.

## Responsibility and explicit non-responsibilities

Own credentials and authentication sessions in future iterations.

It does not own editable public profiles, messages, media objects, realtime delivery, or gateway policy.

## Hexagonal/DDD layer map

- `domain`: framework-free future business model.
- `application`: future use cases and owned ports; depends only on domain.
- `infrastructure`: future adapters implementing application ports.
- `entrypoints`: transport translation that will call application services.
- `core`: configuration and cross-cutting logging only.

The dependency direction is `entrypoints -> application -> domain` and `infrastructure -> application ports -> domain`.

## Entrypoints

- `app.entrypoints.http.main:create_app` - FastAPI factory
- `GET /health/live` - process liveness
- `GET /health/ready` - initialized application readiness

No business API or transport contract is available yet.

## Configuration variables

- `SERVICE_NAME`, `APP_VERSION`, `APP_ENVIRONMENT`
- `HOST`, `PORT`
- `DEV_LOGS`, `LOG_LEVEL`, `MUTE_LOGGERS`

## Liveness and readiness

`GET /health/live` reports that the process is running. `GET /health/ready` reports readiness after application lifespan initialization. It intentionally performs no fake dependency probes.

## Local build and run status

Runtime and test dependencies are declared and locked for Python 3.14. The
service can be verified from this repository with:

```powershell
poetry sync --with dev --no-root
poetry run ruff check .
poetry run ruff format --check .
poetry run pytest
docker build --target runtime --tag andruha/identity-service:local .
```

`.github/workflows/ci.yml` runs lint, strict Pyright, unit and integration
tests, branch coverage >= 80%, runtime dependency audit, secret scanning, and a
Docker smoke test. `.github/workflows/release.yml` publishes a verified image
to GHCR only for a version tag. Business APIs and persistence remain deferred.

## Canonical project material

- [Documentation](https://github.com/yemeal/andruha-messenger/tree/main/docs)
- [Contracts](https://github.com/yemeal/andruha-messenger/tree/main/contracts)
