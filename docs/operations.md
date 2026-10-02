# Operations

Use one absolute state path consistently. All examples use `WAKE_STATE` set to
that path. Registration JSON includes a secret; use `umask 077` on Unix and keep
it private. On Windows, use a dedicated local state directory with Windows ACL
support. Wake Codex protects that directory for the current user before writing
secrets; new files inherit its ACL. See the README for PowerShell commands.

## Service lifecycle

`start` detaches the listener and checks health. It refuses a conflicting
backend/version/runtime rather than silently reconfiguring an existing service.
Only stop a listener you own. `--port 0` selects a new port if the remembered port
is occupied; update external producers if the callback URL changes.

`start --dry-run` saves continuation prompts as `previewed` without sending them.
Use a separate state directory for previews. `doctor` never invokes a model.

The HTTP listener wakes immediately on callbacks. A Python-only 30-second DB
reconciliation catches missed nudges. This is not a recurring model invocation.

## HTTP callbacks

Send JSON to the registration's `service.url + callback_path` with:

```text
Authorization: Bearer <callback_token>
Content-Type: application/json
```

The first valid terminal event wins. Repeated submission returns `accepted: false`.
For remote producers, use an authenticated SSH tunnel; do not expose the local
listener directly on a public interface. The bundled `send_event.py` accepts a
`--url` override for the tunnel endpoint.

If the listener is offline, a local producer can persist the event with:

```sh
python3 scripts/wakecodex.py --state "$WAKE_STATE" emit WAIT_UUID --file result.json
```

Restarting the listener dispatches saved ready events. A recorded in-flight
delivery becomes `needs_attention`: inspect history rather than replaying it.

## Attach to an existing JSONL job

The optional watcher does not edit or restart the job:

```sh
python3 scripts/watch_job.py --state "$WAKE_STATE" --wait-id WAIT_UUID \
  --events /absolute/path/to/events.jsonl --experiment EXACT_EXPERIMENT_ID \
  --minimum-step EXPECTED_TERMINAL_BOUNDARY --pid EXACT_JOB_PID
```

Run it through your process supervisor for long jobs. It uses macOS kernel
file/process notifications; Linux and Windows use lightweight Python waits.
Windows PID checks use a process handle without sending a signal or terminating
the observed process.
It expects records with `type`, `experiment_id` and `step`, for example:

```json
{"type":"complete","experiment_id":"run-17","step":2048,"reason":"budget reached"}
```

Check your trainer's actual schema first. Review/regression completion maps to
`needs_review`; pause/interrupt completion maps to `paused`; explicit error maps
to `failed`. The watcher filters by exact experiment and minimum step, so the
boundary must allow any earlier pause you want to observe. Do not set it to a
future planned final step if early-pause events should count.

The PID must be the actual foreground job, not a detached launcher. An exit
without a matching terminal record is reported as failure requiring inspection.
File rotation/truncation is rejected rather than guessed through. A cursor
preserves progress on watcher restart. The default 24-hour watcher deadline ends
without claiming job completion. Supervise watcher errors and deadlines yourself.

## Legacy CLI-owned sessions

`start --backend resume` retains `codex exec resume` for an explicitly owned,
idle CLI thread. It defaults to a read-only model sandbox and verifies the returned
thread ID and completed-turn event. This backend does not solve desktop ownership
conflicts; do not use it on a desktop-owned chat. Queue is the default and has no
automatic fallback. The old rollout idle helper remains for legacy tests, not as
a workaround for a live writer lock.

## Inspect and recover

Read `show WAIT_UUID`, the delivery receipt and the corresponding `.queue.stdout`
and `.queue.stderr` files. Receipts record queue acceptance separately from turn
completion. `retry WAIT_UUID` is explicit and should follow a check that the prior
attempt did not already enqueue/act. Accepted `queued` waits cannot be retried by
this command. Keep the service state out of public bug reports.

An external event with `status: cancelled` notifies the agent that its job was
cancelled. The local `cancel WAIT_UUID` command instead suppresses a pending wake.
