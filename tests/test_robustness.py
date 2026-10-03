"""Fault injection using temporary state and fake adapters only."""

import concurrent.futures
import contextlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wakecodex as wake


class RobustnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = wake.Store(self.root / "state")
        self.thread = str(uuid.uuid4())

    def tearDown(self):
        self.temp.cleanup()

    def register(self, command=None):
        return self.store.register(self.thread, str(self.root), "Report once.", command=command)

    def race(self, actions):
        barrier = threading.Barrier(len(actions))

        def run(action):
            barrier.wait(timeout=5)
            return action()

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(run, actions))

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for test worker state")

    def test_concurrent_reordered_terminal_callbacks_have_one_winner(self):
        for first in wake.TERMINAL_EVENTS:
            with self.subTest(first=first):
                wait = self.register()
                events = [{"status": first, "sequence": 0}] + [
                    {"status": status, "sequence": i + 1}
                    for i, status in enumerate(wake.TERMINAL_EVENTS)
                ]
                results = self.race(
                    [lambda event=event: self.store.complete(wait["id"], event) for event in events]
                )
                self.assertEqual(sum(results), 1)
                winner = events[results.index(True)]
                reopened = wake.Store(self.store.root)
                self.assertEqual(reopened.get(wait["id"])["event"], winner)
                calls = []
                self.assertTrue(
                    wake.dispatch_once(reopened, lambda w, r: calls.append(w["event"]) or "queued")
                )
                for event in reversed(events):
                    self.assertFalse(reopened.complete(wait["id"], event))
                self.assertFalse(wake.dispatch_once(reopened, Mock()))
                self.assertEqual(calls, [winner])

    def test_competing_dispatchers_claim_each_wait_once(self):
        waits = [self.register() for _ in range(12)]
        for wait in waits:
            self.store.complete(wait["id"], {"status": "completed"})
        calls = []
        lock = threading.Lock()

        def adapter(wait, root):
            with lock:
                calls.append(wait["id"])
            return "queued"

        def drain():
            while wake.dispatch_once(self.store, adapter):
                pass

        self.race([drain] * 6)
        self.assertCountEqual(calls, [wait["id"] for wait in waits])
        self.assertTrue(all(self.store.get(w["id"])["status"] == "queued" for w in waits))

    def test_cancel_and_claim_race_has_only_one_winner(self):
        for _ in range(10):
            wait = self.register()
            self.store.complete(wait["id"], {"status": "completed"})
            cancelled, claimed = self.race(
                [lambda: self.store.cancel(wait["id"]), self.store.claim]
            )
            if cancelled:
                self.assertIsNone(claimed)
                self.assertEqual(self.store.get(wait["id"])["status"], "cancelled")
            else:
                self.assertEqual(claimed["id"], wait["id"])
                self.assertEqual(self.store.get(wait["id"])["status"], "delivering")
                self.store.finish(wait["id"], "queued")
                self.assertFalse(self.store.cancel(wait["id"]))

    def test_event_transaction_rolls_back_when_commit_fails(self):
        wait = self.register()
        original = self.store.connect

        @contextlib.contextmanager
        def fail_commit():
            with original() as db:
                yield db
                raise sqlite3.OperationalError("injected failure before commit")

        with patch.object(self.store, "connect", fail_commit):
            with self.assertRaises(sqlite3.OperationalError):
                self.store.complete(wait["id"], {"status": "completed"})
        self.assertEqual(self.store.get(wait["id"])["status"], "waiting")
        self.assertIsNone(self.store.get(wait["id"])["event"])
        self.assertTrue(self.store.complete(wait["id"], {"status": "failed"}))

    def test_write_lock_failure_preserves_event_and_prevents_delivery(self):
        wait = self.register()
        real_connect = sqlite3.connect

        def short_connect(*args, **kwargs):
            kwargs["timeout"] = 0.01
            return real_connect(*args, **kwargs)

        with self.store.connect() as locked:
            locked.execute("BEGIN IMMEDIATE")
            with patch("wakecodex.sqlite3.connect", side_effect=short_connect):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    self.store.complete(wait["id"], {"status": "completed"})
        self.assertEqual(self.store.get(wait["id"])["status"], "waiting")
        self.assertTrue(self.store.complete(wait["id"], {"status": "completed"}))
        adapter = Mock(return_value="queued")
        with self.store.connect() as locked:
            locked.execute("BEGIN IMMEDIATE")
            with patch("wakecodex.sqlite3.connect", side_effect=short_connect):
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    wake.dispatch_once(self.store, adapter)
        adapter.assert_not_called()
        self.assertEqual(self.store.get(wait["id"])["status"], "ready")
        self.assertTrue(wake.dispatch_once(self.store, adapter))
        adapter.assert_called_once()

    def crash(self, wait, phase):
        # os._exit bypasses all cleanup: only committed SQLite state survives.
        script = """
import os, sys
from pathlib import Path
from wakecodex import Store
store = Store(sys.argv[1])
wait_id, phase = sys.argv[2:]
if phase == 'before_event':
    with store.connect() as db:
        db.execute("UPDATE waits SET event=?, status='ready' WHERE id=?",
                   ('{\"status\":\"completed\"}', wait_id))
        os._exit(0)
store.complete(wait_id, {'status': 'completed'})
if phase != 'after_event':
    store.claim()
if phase == 'after_queue':
    (store.root / 'fake-enqueue').write_text(wait_id)
os._exit(0)
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(self.store.root), wait["id"], phase],
            cwd=Path(wake.__file__).parent,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(child.returncode, 0, child.stderr)

    def test_process_crashes_at_event_and_delivery_boundaries(self):
        for phase in ("before_event", "after_event", "after_claim", "after_queue"):
            with self.subTest(phase=phase):
                wait = self.register()
                self.crash(wait, phase)
                reopened = wake.Store(self.store.root)
                reopened.recover()
                adapter = Mock(return_value="queued")
                if phase == "before_event":
                    self.assertEqual(reopened.get(wait["id"])["status"], "waiting")
                    self.assertFalse(wake.dispatch_once(reopened, adapter))
                    self.assertTrue(reopened.complete(wait["id"], {"status": "failed"}))
                    reopened.cancel(wait["id"])
                elif phase == "after_event":
                    self.assertTrue(wake.dispatch_once(reopened, adapter))
                    self.assertEqual(reopened.get(wait["id"])["status"], "queued")
                    adapter.assert_called_once()
                else:
                    self.assertEqual(reopened.get(wait["id"])["status"], "needs_attention")
                    self.assertFalse(wake.dispatch_once(reopened, adapter))
                    adapter.assert_not_called()
                    if phase == "after_queue":
                        self.assertEqual((reopened.root / "fake-enqueue").read_text(), wait["id"])
                    reopened.cancel(wait["id"])
                self.assertFalse(wake.dispatch_once(reopened, Mock()))

    def test_failed_status_commit_after_enqueue_does_not_auto_retry(self):
        wait = self.register()
        self.store.complete(wait["id"], {"status": "completed"})
        adapter = Mock(return_value="queued")
        with patch.object(self.store, "finish", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(sqlite3.OperationalError):
                wake.dispatch_once(self.store, adapter)
        adapter.assert_called_once()
        self.assertEqual(self.store.get(wait["id"])["status"], "delivering")
        self.store.recover()
        self.assertFalse(wake.dispatch_once(self.store, adapter))
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(self.store.get(wait["id"])["status"], "needs_attention")

    def test_explicit_retry_preserves_prior_queue_evidence(self):
        wait = self.register()
        self.store.complete(wait["id"], {"status": "completed"})
        message_id = str(uuid.uuid4())

        def acknowledge(args, **kwargs):
            kwargs["stdout"].write(f"Queued message {message_id} for thread {self.thread}.\n")
            return subprocess.CompletedProcess(args, 0)

        adapter = wake.QueueAdapter()
        with patch("wakecodex.subprocess.run", side_effect=subprocess.TimeoutExpired("fake", 1)):
            wake.dispatch_once(self.store, adapter)
        self.assertEqual(self.store.get(wait["id"])["status"], "needs_attention")
        old_prompt = (self.store.root / f"{wait['id']}.prompt.txt").read_text()
        self.assertTrue(self.store.retry(wait["id"]))
        self.assertFalse(self.store.retry(wait["id"]))
        with patch("wakecodex.subprocess.run", side_effect=acknowledge) as run:
            wake.dispatch_once(self.store, adapter)
            run.assert_called_once()
        self.assertEqual(self.store.get(wait["id"])["status"], "queued")
        self.assertFalse(self.store.retry(wait["id"]))
        previous = list(self.store.root.glob(f"{wait['id']}.previous-*.prompt.txt"))
        self.assertEqual(len(previous), 1)
        self.assertEqual(previous[0].read_text(), old_prompt)
        receipt = json.loads((self.store.root / f"{wait['id']}.delivery.json").read_text())
        self.assertTrue(receipt["queue_accepted"])
        self.assertFalse(receipt["turn_completed"])

    def test_http_storage_failure_is_retryable_without_duplicate_event(self):
        wait = self.register()
        server, worker, trigger, stop = wake.make_server(
            self.store, wake.QueueAdapter(dry_run=True)
        )
        http = threading.Thread(target=server.serve_forever, daemon=True)
        http.start()
        endpoint = json.loads((self.store.root / "endpoint.json").read_text())["url"]

        def post():
            request = urllib.request.Request(
                endpoint + "/events/" + wait["id"],
                data=b'{"status":"completed"}',
                headers={"Authorization": "Bearer " + wait["callback_token"]},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        try:
            with patch.object(
                self.store, "complete", side_effect=sqlite3.OperationalError("locked")
            ):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post()
                self.assertEqual(error.exception.code, 503)
                error.exception.close()
            self.assertEqual(self.store.get(wait["id"])["status"], "waiting")
            self.assertTrue(post()["accepted"])
            self.assertFalse(post()["accepted"])
            self.assertEqual(self.store.get(wait["id"])["event"], {"status": "completed"})
        finally:
            server.shutdown()
            http.join(timeout=5)
            server.server_close()

    def test_dispatcher_survives_storage_error_and_recovers_on_nudge(self):
        wait = self.register()
        self.store.complete(wait["id"], {"status": "completed"})
        server, worker, trigger, stop = wake.make_server(
            self.store, wake.QueueAdapter(dry_run=True)
        )
        http = threading.Thread(target=server.serve_forever, daemon=True)
        http.start()
        failed = threading.Event()
        original_claim = self.store.claim

        def claim():
            if not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("injected lock contention")
            return original_claim()

        try:
            with patch.object(self.store, "claim", side_effect=claim):
                worker.start()
                self.assertTrue(failed.wait(timeout=2))
                self.wait_until(lambda: wake.service_health(self.store).get("error"))
                health = wake.service_health(self.store)
                self.assertFalse(health["healthy"])
                self.assertIn("lock contention", health["error"])
                self.assertTrue(worker.is_alive())
                trigger.set()
                self.wait_until(lambda: self.store.get(wait["id"])["status"] == "previewed")
                self.wait_until(lambda: wake.service_health(self.store).get("healthy"))
                self.assertEqual(self.store.get(wait["id"])["status"], "previewed")
                self.assertTrue(wake.service_health(self.store)["healthy"])
        finally:
            stop.set()
            trigger.set()
            worker.join(timeout=5)
            server.shutdown()
            http.join(timeout=5)
            server.server_close()

    def test_wrapper_storage_failure_keeps_claim_and_requires_manual_result(self):
        wait = self.register(command=["fake-job"])
        with (
            patch(
                "wakecodex.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
            ) as run,
            patch.object(self.store, "complete", side_effect=sqlite3.OperationalError("locked")),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                wake.run_job(self.store, wait["id"])
            run.assert_called_once()
        self.store.recover()
        self.assertEqual(self.store.get(wait["id"])["status"], "waiting")
        with patch("wakecodex.subprocess.run") as run:
            wake.run_job(self.store, wait["id"])
            run.assert_not_called()
        self.assertTrue(self.store.complete(wait["id"], {"status": "completed", "exit_code": 0}))

    def test_worker_failures_and_persistent_claim_never_restart_job(self):
        for error in (FileNotFoundError("missing program"), subprocess.TimeoutExpired("fake", 1)):
            with self.subTest(error=type(error).__name__):
                wait = self.register(command=["fake-job"])
                with patch("wakecodex.subprocess.run", side_effect=error) as run:
                    wake.run_job(self.store, wait["id"])
                    wake.run_job(self.store, wait["id"])
                    run.assert_called_once()
                self.assertEqual(self.store.get(wait["id"])["event"]["status"], "failed")
        wait = self.register(command=["fake-job"])
        (self.store.root / f"{wait['id']}.job-started.json").write_text('{"pid": 1}')
        with patch("wakecodex.subprocess.run") as run:
            wake.run_job(self.store, wait["id"])
            run.assert_not_called()
        self.assertEqual(self.store.get(wait["id"])["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
