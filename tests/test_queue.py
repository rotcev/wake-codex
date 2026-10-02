import json
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
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wakecodex as wake
from platform_support import require_process


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = wake.Store(self.root / "state")
        self.thread = str(uuid.uuid4())
        self.wait = self.store.register(self.thread, str(self.root), "Report once, then stop.")
        self.store.complete(self.wait["id"], {"status": "completed"})
        self.wait = self.store.get(self.wait["id"])

    def tearDown(self):
        self.temp.cleanup()

    def acknowledge(self, args, **kwargs):
        self.assertEqual(args[1], "queue")
        self.assertEqual(args[args.index("--thread") + 1], self.thread)
        kwargs["stdout"].write(f"Queued message {uuid.uuid4()} for thread {self.thread}.\n")
        return subprocess.CompletedProcess(args, 0)

    def test_queue_ack_is_not_model_completion(self):
        with patch("wakecodex.subprocess.run", side_effect=self.acknowledge):
            wake.dispatch_once(self.store, wake.QueueAdapter())
        self.assertEqual(self.store.get(self.wait["id"])["status"], "queued")
        receipt = json.loads((self.store.root / (self.wait["id"] + ".delivery.json")).read_text())
        self.assertTrue(receipt["queue_accepted"])
        self.assertFalse(receipt["turn_completed"])
        self.assertFalse(receipt["desktop_display_verified"])
        self.assertFalse(self.store.retry(self.wait["id"]))

    def test_mismatched_queue_target_requires_attention(self):
        def other(args, **kwargs):
            kwargs["stdout"].write(f"Queued message {uuid.uuid4()} for thread {uuid.uuid4()}.")
            return subprocess.CompletedProcess(args, 0)

        with patch("wakecodex.subprocess.run", side_effect=other):
            wake.dispatch_once(self.store, wake.QueueAdapter())
        self.assertEqual(self.store.get(self.wait["id"])["status"], "needs_attention")

    def test_queue_timeout_never_falls_back_to_resume(self):
        with patch(
            "wakecodex.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 1)
        ) as run:
            wake.dispatch_once(self.store, wake.QueueAdapter())
            self.assertFalse(wake.dispatch_once(self.store, wake.QueueAdapter()))
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.store.get(self.wait["id"])["status"], "needs_attention")

    def test_no_model_override_in_queue_mode(self):
        self.wait["model"] = "explicit-override"
        with patch("wakecodex.subprocess.run") as run:
            with self.assertRaises(ValueError):
                wake.QueueAdapter()(self.wait, self.store.root)
            run.assert_not_called()

    def test_preview_does_not_queue(self):
        with patch("wakecodex.subprocess.run") as run:
            wake.dispatch_once(self.store, wake.QueueAdapter(dry_run=True))
            run.assert_not_called()
        self.assertEqual(self.store.get(self.wait["id"])["status"], "previewed")

    def test_detached_cli_service_wraps_job_and_queues_result(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "wakecodex.py"
        fake = Path(__file__).with_name("fake_codex.py").resolve()
        state = self.root / "detached"

        def command(*args):
            result = subprocess.run(
                [sys.executable, str(script), "--state", str(state), *args],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        health = command("start", "--codex", str(fake))
        try:
            self.assertTrue(health["healthy"])
            self.assertEqual(health["backend"], "queue")
            again = command("start", "--codex", str(fake))
            self.assertEqual(health["pid"], again["pid"])
            registration = command(
                "submit",
                "--thread",
                self.thread,
                "--cwd",
                str(self.root),
                "--then",
                "Report once.",
                "--",
                sys.executable,
                "-c",
                "print('finished')",
            )
            store = wake.Store(state)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                saved = store.get(registration["id"])
                if saved["status"] == "queued":
                    break
                time.sleep(0.02)
            self.assertEqual(saved["status"], "queued")
            self.assertEqual(saved["event"]["exit_code"], 0)
        finally:
            command("stop")
            deadline = time.monotonic() + 5
            while (state / "endpoint.json").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse((state / "endpoint.json").exists())
            # Windows cannot unlink redirected logs until the process closes them.
            while time.monotonic() < deadline:
                try:
                    require_process(health["pid"])
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("Listener did not exit after stop")

    def test_http_to_real_subprocess_queue_and_duplicate_suppression(self):
        # A real HTTP listener and queue subprocess, but deliberately no AI.
        fake = Path(__file__).with_name("fake_codex.py")
        server, worker, trigger, stop = wake.make_server(self.store, wake.QueueAdapter(str(fake)))
        http = threading.Thread(target=server.serve_forever, daemon=True)
        http.start()
        w = self.store.register(self.thread, str(self.root), "Report the result.")
        endpoint = json.loads((self.store.root / "endpoint.json").read_text())["url"]

        def post(token):
            req = urllib.request.Request(
                endpoint + "/events/" + w["id"],
                data=b'{"status":"needs_review"}',
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                return json.load(response)

        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                post("wrong-token")
            self.assertEqual(denied.exception.code, 401)
            denied.exception.close()
            self.assertTrue(post(w["callback_token"])["accepted"])
            self.assertFalse(post(w["callback_token"])["accepted"])
            self.assertEqual(self.store.get(w["id"])["status"], "ready")
            worker.start()
            deadline = time.monotonic() + 5
            while (
                self.store.get(w["id"])["status"] in ("ready", "delivering")
                and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            self.assertEqual(self.store.get(w["id"])["status"], "queued")
            self.assertFalse(post(w["callback_token"])["accepted"])
            self.assertFalse(self.store.retry(w["id"]))
        finally:
            stop.set()
            trigger.set()
            server.shutdown()
            http.join()
            server.server_close()
            if worker.ident:
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
