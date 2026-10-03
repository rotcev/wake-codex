# Verification

## The live result

On October 1, 2026, using Codex CLI **0.159.2** and the desktop app:

1. `exec resume` against an idle desktop chat failed with an active-writer conflict.
2. `codex queue` acknowledged a message addressed to that same chat.
3. The user saw the queued message on the paired phone. That first attempt was
   manually sent and stopped; it was **not** automatic-delivery evidence.
4. A second message was queued and the submitting agent immediately ended its turn.
5. The test message arrived as the next user input and the agent produced the
   exact requested acknowledgement: **“Wake Codex automatic queue test received.”**

That is evidence for the native queue transport in this tested setup—not a
claim of universal client compatibility or completion of arbitrary jobs.
Private thread IDs, transcripts and local paths are intentionally not published.

## Release checks

Version 0.4.0 integrates that transport as the service's default. Automated tests
exercise the service → queue subprocess boundary using a test double, so tests
do not spend model tokens. Coverage includes terminal states, duplicate suppression,
durable restart, real worker exit, file event watching, authentication, matching
queue acknowledgments, cancellation, and no automatic retry after ambiguity.

The integrated dummy-job → listener → native queue → same desktop chat →
automatic reply chain was tested live on the Windows setup described below.
The earlier October 1 test established native queue transport only. Neither
test establishes integrated live coverage on every OS, client version, or host;
the cross-platform service tests use a fake queue and make no model calls.

## Boundaries

- No guarantee of exactly-once execution across process crashes. A process can
  die after sending a message but before recording its acknowledgment.
- No guarantee that every Codex app/CLI version consumes external queued messages.
- No test of restoring running jobs after a host reboot.
- Windows requires a local filesystem with ACL support and a native Codex executable.
- Queue mode inherits existing session settings; it is not a new security sandbox.
- A model can fail after receiving a correct callback. Inspect its result.

## Windows port verification

The native Windows test suite was exercised with Python 3.12 and a fake Codex
queue: detached start/health/stop, wrapped job completion, authenticated HTTP
callbacks, duplicate suppression, lock contention/release, process liveness,
private inherited state ACLs, and Unicode continuations. These tests make no
model calls. The CI matrix includes Windows with Python 3.10 and 3.13; adding
those jobs is not evidence that hosted CI has passed.

The desktop-matching native Codex CLI 0.159.2 supports `queue --help` on the
Windows test host. A separately authorized live dummy job completed, queued its
continuation into the same desktop chat, and produced the requested automatic
reply. This verifies that one Windows desktop setup; other client versions and
remote hosts still require their own live check.
