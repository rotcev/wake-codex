# Robustness audit

This audit starts from main `b137f7e` and adds fault-injection tests without
changing runtime logic, architecture, or public states. All new tests use
isolated temporary state and fake queue adapters; none call a model or a real
chat. The process-crash tests use `os._exit` to bypass cleanup.

## What is idempotent

A wait accepts only its first committed valid terminal event. That includes
`completed`, `failed`, `paused`, `needs_review`, and external `cancelled`.
Concurrent, duplicated, or reordered events cannot overwrite the winner or
create another delivery for that wait. "First" means the update that commits
first, not the earliest producer timestamp. Later corrections need inspection;
they do not replace the accepted result.

SQLite's `BEGIN IMMEDIATE` serializes delivery claims. Competing dispatchers
cannot claim the same ready wait twice. The service also holds an OS process
lock; existing platform tests cover lock contention and release after exit.
Local cancellation and claiming race atomically: cancellation suppresses a
pending wake if it wins, but cannot retract an in-flight or queued message.

This provides at-most-once enqueue **intent per claim**, not exactly-once model
execution. `queued` establishes only a matching queue acknowledgment. Actual
consumption, model completion, and useful work still need separate evidence.

## Tested fault boundaries

| Injected condition | Verified result |
| --- | --- |
| Concurrent mixed terminal events, duplicates after restart/delivery | One accepted event and one adapter call |
| Six competing dispatchers draining twelve waits | Each wait claimed once |
| Cancellation racing delivery claim | Either cancelled without a claim, or claimed without cancellation |
| Event transaction failure before commit, process exit before commit | Rollback leaves `waiting`; producer can submit again |
| Process exit after event commit but before claim | Restart retains ready event and dispatches once |
| Process exit after claim, before or after simulated enqueue | Restart marks `needs_attention`; no automatic replay |
| Queue accepted but saving final delivery status fails | Remains claimed, then requires attention on restart; no automatic replay |
| SQLite write-lock timeout during callback or claim | No partial update or adapter call; successful retry after lock release |
| HTTP callback storage error | HTTP 503, then successful producer retry with duplicate suppression |
| Dispatcher storage error | Dispatcher survives, health reports the error, a nudge restores progress |
| Queue timeout followed by explicit retry | No automatic retry; explicit retry preserves prior attempt evidence |
| Missing job executable or injected subprocess timeout | A failed event is recorded; job is not restarted |
| Persistent job claim with no completion event | Job does not run again; wait remains pending for inspection |
| Job completes but storing its result fails | Wrapper error propagates; claim prevents rerun; explicit result can recover wait |

Existing tests additionally exercise real worker nonzero exit, localhost
callback authentication, queue target mismatch, queue acceptance versus turn
completion, detached lifecycle, watcher event filtering, and platform locks.
The injected job timeout checks exception handling only: wrapped jobs have no
configured runtime deadline. Delivery CLI timeouts do not bound job duration.

## Residual risks and recovery

- A crash after enqueue but before acknowledgment persistence is ambiguous.
  Explicit `retry` can duplicate an already accepted message or action. Inspect
  queue/session history and saved attempt logs before retrying. `queued` waits
  cannot be retried through the local command.
- A crash after creating the persistent job claim but before launching the job,
  or before recording its result, can leave `waiting` indefinitely. Listener
  restart does not infer the actual job outcome or restart it. Inspect the
  `.job-started.json`, `.worker.log`, `.job.log`, and actual artifacts; emit the
  known result or cancel the wait. Do not delete the claim to rerun blindly.
- SQLite waits up to ten seconds for locks. HTTP reports storage failures as
  retryable 503 responses; a wrapper that cannot persist its result exits with
  an error. Supervisor/operator recovery is necessary for that pending wait.
- These are process-crash and injected storage-failure tests, not proof against
  disk corruption, hardware power loss, network filesystems, or restoring jobs
  after reboot. Use a supported local filesystem and keep state private.
- Runtime duplicate suppression cannot make downstream model actions idempotent.
  No downstream idempotency key contract or exactly-once execution is promised.
