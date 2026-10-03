"""Opt-in agent Goal handoff; never called by the listener or queue adapter."""

import json
import os
import queue
import subprocess
import tempfile
import threading
import time

from platform_support import executable_command, process_options


class GoalClient:
    """Short-lived public API client; never resumes or takes ownership of a thread."""

    def __init__(self, executable, timeout=10):
        self.executable, self.timeout = executable, timeout
        self.messages = queue.Queue()
        self.next_id = 0

    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(
                [*executable_command(self.executable), "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
                text=True,
                encoding="utf-8",
                **process_options(),
            )
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self.call(
                "initialize",
                {
                    "clientInfo": {"name": "wake_codex_goal_wait", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (ValueError, OSError) as exc:
            self.messages.put(exc)
        finally:
            self.messages.put(None)

    def _send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if message is None or isinstance(message, Exception):
                raise RuntimeError("Goal API connection ended without an acknowledgment")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError("Goal API rejected " + method + ": " + str(message["error"]))
            if "result" not in message:
                raise RuntimeError("Goal API response has no result")
            return message["result"]
        raise TimeoutError("Goal API timed out during " + method + "; inspect before retrying")

    def get(self, thread):
        result = self.call("thread/goal/get", {"threadId": thread})
        goal = result.get("goal")
        if goal is not None and goal.get("threadId") != thread:
            raise RuntimeError("Goal API returned a different thread")
        return goal

    def set_status(self, thread, status):
        # Omit objective and budget so usage history and owner settings survive.
        result = self.call("thread/goal/set", {"threadId": thread, "status": status})
        goal = result.get("goal")
        if not goal or goal.get("threadId") != thread or goal.get("status") != status:
            raise RuntimeError("Goal API did not acknowledge the requested state")
        return goal

    def close(self):
        process = getattr(self, "process", None)
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            reader = getattr(self, "reader", None)
            if reader is not None:
                reader.join(timeout=2)
            process.stdout.close()
        errors = getattr(self, "errors", None)
        if errors is not None:
            errors.close()

    def __exit__(self, *_):
        self.close()


def capture(client, thread, next_step, output, authorized_resume=False):
    """Save trusted owner intent AFTER an explicitly authorized pause."""
    import uuid
    from pathlib import Path

    thread = str(uuid.UUID(thread))
    if not authorized_resume or not next_step.strip():
        raise ValueError("Explicit conditional-resume authorization and next step are required")
    goal = client.get(thread)
    if not goal or goal.get("status") != "paused":
        raise ValueError("Capture requires an existing intentionally paused Goal")
    record = {
        "version": 1,
        "thread_id": thread,
        "intent_id": str(uuid.uuid4()),
        "conditional_resume_authorized": True,
        "next_step": next_step,
        "expected_goal": goal,
    }
    # Never overwrite another handoff; callback producers must not control this path.
    descriptor = os.open(Path(output), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    return record


def inspect(client, record):
    """Read-only comparison. This result never authorizes or performs activation."""
    expected = record.get("expected_goal")
    thread = record.get("thread_id")
    if (
        record.get("version") != 1
        or record.get("conditional_resume_authorized") is not True
        or not record.get("next_step")
        or not isinstance(expected, dict)
        or expected.get("threadId") != thread
        or expected.get("status") != "paused"
    ):
        raise ValueError("Invalid trusted Goal handoff")
    current = client.get(thread)
    return {
        "unchanged_paused_goal": current == expected,
        "current_goal": current,
        "next_step": record["next_step"],
        "comparison_only": True,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, help="Absolute desktop-matching CLI path")
    sub = parser.add_subparsers(dest="action", required=True)
    save = sub.add_parser("capture")
    save.add_argument("--thread", required=True)
    save.add_argument("--next-step", required=True)
    save.add_argument("--output", required=True)
    save.add_argument("--authorized-resume", action="store_true")
    read = sub.add_parser("inspect")
    read.add_argument("--handoff", required=True)
    args = parser.parse_args()
    with GoalClient(args.codex) as client:
        if args.action == "capture":
            record = capture(
                client, args.thread, args.next_step, args.output, args.authorized_resume
            )
            print(json.dumps({"saved": True, "intent_id": record["intent_id"]}))
        else:
            with open(args.handoff, encoding="utf-8") as stream:
                print(json.dumps(inspect(client, json.load(stream))))


if __name__ == "__main__":
    main()
