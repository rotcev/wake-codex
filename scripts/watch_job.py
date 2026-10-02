"""Attach to an existing JSONL job without editing or restarting it.

macOS uses file-write/process-exit kernel notifications; waiting never invokes
a model. The persisted WakeCodex wait owns delivery and duplicate suppression.
"""

import argparse
import json
import os
import select
import time
from pathlib import Path

import wakecodex as wake
from idle_gate import FileSignal
from platform_support import require_process


def terminal_event(row, experiment, minimum_step):
    if row.get("experiment_id") != experiment or row.get("step", -1) < minimum_step:
        return None
    kind = row.get("type")
    reason = str(row.get("reason", ""))
    if kind == "complete":
        status = (
            "needs_review"
            if "review" in reason.lower() or "regression" in reason.lower()
            else "completed"
        )
        if any(x in reason.lower() for x in ("pause", "interrupt", "signal")):
            status = "paused"
    elif kind in ("paused", "failed", "cancelled", "needs_review"):
        status = kind
    elif kind == "error":
        status = "failed"
    else:
        return None
    return dict(
        status=status,
        experiment_id=experiment,
        step=row.get("step"),
        reason=reason,
        evidence_kind="job_terminal_event",
    )


def watch(store, wait_id, path, experiment, minimum_step, pid=None, timeout=86400):
    path = Path(path).resolve(strict=True)
    store.get(wait_id)
    cursor = store.root / (wait_id + ".watch.json")
    if pid:
        require_process(pid)  # Permission failure is not evidence that a process died.
    signal = FileSignal(path)
    if pid:
        if signal.queue:
            signal.queue.control(
                [
                    select.kevent(
                        pid,
                        filter=select.KQ_FILTER_PROC,
                        flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                        fflags=select.KQ_NOTE_EXIT,
                    )
                ],
                0,
            )
    started = time.monotonic()
    stat = path.stat()
    if cursor.exists():
        saved = json.loads(cursor.read_text())
        if (saved["path"], saved["experiment"], saved["inode"]) != (
            str(path),
            experiment,
            stat.st_ino,
        ):
            raise ValueError("Watched job identity changed; review before resuming")
        offset = saved["offset"]
    else:
        # Catch a completion that raced with registration. Only inspect the tail,
        # match exact experiment and boundary; never infer success from launch exit.
        offset = max(0, stat.st_size - 256 * 1024)
        if offset:
            with path.open("rb") as f:
                f.seek(offset)
                f.readline()
                offset = f.tell()
    exit_seen = False
    try:
        while True:
            st = path.stat()
            if st.st_ino != stat.st_ino or st.st_size < offset:
                raise RuntimeError("Event file rotated/truncated; refusing ambiguous replay")
            event = None
            with path.open("rb") as f:
                f.seek(offset)
                while True:
                    line = f.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    row = json.loads(line)
                    offset = f.tell()
                    event = terminal_event(row, experiment, minimum_step)
                    if event:
                        break
            # First terminal event commits to SQLite before moving its durable
            # file cursor: a crash between these writes is a harmless duplicate.
            if event:
                event["events_path"] = str(path)
                accepted = store.complete(wait_id, event)
                wake.poke(store)
                result = dict(status="event_persisted", accepted=accepted, event=event)
            else:
                result = None
            tmp = cursor.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    dict(path=str(path), experiment=experiment, inode=stat.st_ino, offset=offset)
                )
            )
            tmp.replace(cursor)
            if result:
                return result
            if store.get(wait_id)["status"] != "waiting":
                return dict(status="wait_no_longer_pending")
            if pid:
                try:
                    require_process(pid)
                except ProcessLookupError:
                    if not exit_seen:
                        # Drain a final record written between the last read
                        # and process exit before reporting a missing record.
                        exit_seen = True
                        continue
                    event = dict(
                        status="failed",
                        evidence_kind="process_exit_without_terminal_record",
                        experiment_id=experiment,
                        events_path=str(path),
                        pid=pid,
                        reason="Job process exited without a matching terminal event. Inspect checkpoint; no restart authorized.",
                    )
                    accepted = store.complete(wait_id, event)
                    wake.poke(store)
                    return dict(status="event_persisted", accepted=accepted, event=event)
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return dict(
                    status="watch_deadline",
                    reason="No terminal evidence; do not claim completion or restart job",
                )
            signal.wait(min(60, remaining))
    finally:
        signal.close()


if __name__ == "__main__":
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "wait-id", "events", "experiment"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--minimum-step", type=int, required=True)
    p.add_argument("--pid", type=int)
    p.add_argument("--timeout", type=float, default=86400)
    a = p.parse_args()
    result = watch(
        wake.Store(a.state), a.wait_id, a.events, a.experiment, a.minimum_step, a.pid, a.timeout
    )
    print(json.dumps(result, indent=2))
