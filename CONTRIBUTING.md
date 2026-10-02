# Contributing

Keep Wake Codex small, inspectable, and predictable. It is a durable event relay,
not an autonomous scheduler or an agent framework.

## Local checks

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python -m unittest discover -s tests -v
```

Tests bind localhost and launch a fake CLI. They must never use real model
inference, change a real chat, or depend on credentials. Use temporary state.
Manual live tests need explicit approval from the owner of the target chat.

## Layout

- `scripts/wakecodex.py`: durable store, delivery adapters, HTTP listener and CLI.
- `scripts/send_event.py`: authenticated producer-side callback client.
- `scripts/watch_job.py`: optional adapter for existing JSONL job logs.
- `scripts/idle_gate.py`: file notifications and legacy idle-session diagnostics.
- `scripts/platform_support.py`: OS locks, process options, liveness and private state.
- `tests/`: state-machine, adapter, worker and HTTP integration tests.
- `SKILL.md`: instructions for the Codex agent, kept separate from user docs.

## Invariants worth protecting

1. Waiting never calls a model.
2. A wait accepts only its first terminal event.
3. Queue acknowledgement is not agent completion or task success.
4. An uncertain delivery never auto-retries or silently changes transport.
5. Callback content is untrusted data, not new authorization.
6. Cancelling a notification does not cancel its job.
7. Desktop writer ownership is respected; no lock-file deletion or private IPC tricks.
8. Private state, tokens and user transcripts never enter the repository.

Add behavioral tests for changes to these invariants. Keep runtime dependencies
at zero unless a concrete benefit justifies revisiting that constraint. Changes
to the CLI acknowledgment format should fail closed and receive a focused test.

Please report your Codex version and backend with bug reports, but redact tokens,
prompt text, private paths and thread identifiers. Include only the smallest
reproduction needed. Pull requests should explain what was actually verified.
