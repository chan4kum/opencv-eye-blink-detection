# Contributing

Thanks for helping! This project keeps a high bar because it is meant to be production-grade.

## Setup

```bash
uv sync --all-groups          # Python 3.12+; uv installs the locked dependencies
uv run pre-commit install
make test                     # unit tests with the fake model (any OS)
make test-linux               # everything, including real MediaPipe, in a Linux container
make up && make test-integration   # integration tests against local NATS + S3
```

## Before opening a PR

* `make lint` (ruff, ruff format, mypy --strict) and `make test` pass (plus `make test-linux` if you touched the landmarker, video or stream code). CI additionally enforces 90% combined coverage with the real model.
* Behaviour changes come with tests; bug fixes come with a regression test.
* Update `CHANGELOG.md` (Unreleased) and the docs if user-visible behaviour, configuration or metrics change.
* Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` ...).
* Significant design changes get an ADR in `docs/adr/`.

## Design principles

1. **Fail fast and safe**: validate config at start-up; validate inputs before spending CPU or memory.
2. **Stateless API, scale by replicas**: keep state in NATS / object storage, never in process memory.
3. **Idempotent, at-least-once processing**: persist results before acknowledging messages.
4. **No secrets in code, images or logs**; only digests of API keys are configured.
5. **Untrusted media is hostile**: validate from headers first, then enforce limits on what is actually decoded.
6. **Observable by default**: every new failure mode gets a metric, a log event and a runbook entry.
