import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import platform_support as platform
import wakecodex as wake


class PlatformTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows batch shims")
    def test_batch_shim_is_rejected_without_execution(self):
        for name in ("codex.cmd", "codex.bat"):
            with self.assertRaisesRegex(ValueError, "native codex.exe"):
                platform.executable_command(name)

    def test_lock_contention_and_release_after_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.lock"
            script = (
                "import sys; from platform_support import lock_file; "
                "f=open(sys.argv[1], 'a+b'); lock_file(f)"
            )
            env = {**os.environ, "PYTHONPATH": str(Path(platform.__file__).parent)}
            with path.open("a+b") as stream:
                platform.lock_file(stream)
                child = subprocess.run(
                    [sys.executable, "-c", script, str(path)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    **platform.process_options(),
                )
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("BlockingIOError", child.stderr)
            child = subprocess.run(
                [sys.executable, "-c", script, str(path)],
                env=env,
                capture_output=True,
                timeout=5,
                **platform.process_options(),
            )
            self.assertEqual(child.returncode, 0, child.stderr)
            with path.open("a+b") as stream:
                platform.lock_file(stream)

    def test_process_check_does_not_terminate_job(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            **platform.process_options(),
        )
        try:
            platform.require_process(child.pid)
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=5)
        with self.assertRaises(ProcessLookupError):
            platform.require_process(child.pid)

    def test_invalid_pid_is_rejected(self):
        for pid in (0, -1):
            with self.assertRaises(ValueError):
                platform.require_process(pid)

    def test_privacy_failure_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with patch("wakecodex.private_directory", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    wake.Store(root)
            self.assertFalse((root / "state.sqlite3").exists())

    def test_job_cannot_run_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            store = wake.Store(Path(directory) / "state")
            output = Path(directory) / "count.txt"
            wait = store.register(
                str(uuid.uuid4()),
                directory,
                "Report once",
                command=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import sys; "
                    "p=Path(sys.argv[1]); p.write_text(p.read_text()+'x' if p.exists() else 'x')",
                    str(output),
                ],
            )
            wake.run_job(store, wait["id"])
            wake.run_job(store, wait["id"])
            self.assertEqual(output.read_text(), "x")

    def test_unicode_continuation_through_real_queue_subprocess(self):
        with tempfile.TemporaryDirectory(prefix="wake space ") as directory:
            store = wake.Store(Path(directory) / "state")
            wait = store.register(str(uuid.uuid4()), directory, "Report: \u6d4b\u8bd5 \U0001f680")
            store.complete(wait["id"], {"status": "completed"})
            fake = Path(__file__).with_name("fake_codex.py")
            wake.dispatch_once(store, wake.QueueAdapter(str(fake)))
            self.assertEqual(store.get(wait["id"])["status"], "queued")
            prompt = (store.root / f"{wait['id']}.prompt.txt").read_text(encoding="utf-8")
            self.assertIn("\u6d4b\u8bd5 \U0001f680", prompt)

    @unittest.skipUnless(os.name == "nt", "Windows ACLs")
    def test_windows_state_acl_is_private_and_inherited(self):
        with tempfile.TemporaryDirectory() as directory:
            store = wake.Store(Path(directory) / "state")
            script = (
                "$ErrorActionPreference = 'Stop'; $root = [System.IO.Directory]::GetAccessControl($args[0]); "
                "$child = [System.IO.File]::GetAccessControl($args[1]); "
                "$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
                "@{protected=$root.AreAccessRulesProtected; "
                "root=@($root.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
                "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }); "
                "child=@($child.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object { "
                "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value }); "
                "sid=$sid} | ConvertTo-Json -Compress"
            )
            # Fixed script; paths are supplied as arguments, never interpolated as code.
            check = Path(directory) / "check.ps1"
            check.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-File",
                    str(check),
                    str(store.root),
                    str(store.root / "state.sqlite3"),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
                **platform.process_options(),
            )
            acl = json.loads(result.stdout)
            self.assertTrue(acl["protected"], result.stderr)
            self.assertEqual(acl["root"], [acl["sid"]])
            self.assertEqual(acl["child"], [acl["sid"]])


if __name__ == "__main__":
    unittest.main()
