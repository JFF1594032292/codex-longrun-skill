from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "skill" / "longrun" / "scripts" / "run_and_notify.sh"


def wait_for(path: Path, timeout: float = 12.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def parse_started(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if "job_id" not in values:
        raise AssertionError(f"missing job_id in launcher output:\n{stdout}")
    return values


class LongrunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="codex-longrun-test-")
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.queue_log = self.root / "queue.log"
        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text(
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$FAKE_CODEX_QUEUE_LOG\"\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_codex.chmod(0o755)
        self.env = os.environ.copy()
        self.env["PATH"] = str(self.bin_dir) + os.pathsep + self.env.get("PATH", "")
        self.env["CODEX_THREAD_ID"] = "test-thread-id"
        self.env["FAKE_CODEX_QUEUE_LOG"] = str(self.queue_log)
        self.state_dir = self.root / "state"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def launch(self, name: str, command: list[str], *, extra: list[str] | None = None, cwd: Path | None = None):
        argv = [str(WRAPPER), "--name", name, "--state-dir", str(self.state_dir)]
        if extra:
            argv.extend(extra)
        argv.extend(["--", *command])
        cp = subprocess.run(argv, cwd=cwd, env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, msg=cp.stdout + cp.stderr)
        info = parse_started(cp.stdout)
        job_dir = Path(info["job_dir"])
        return info, job_dir

    def wait_terminal(self, job_dir: Path) -> tuple[str, int]:
        wait_for(job_dir / "COMPLETE")
        state = (job_dir / "state").read_text().strip()
        rc = int((job_dir / "exit_code").read_text().strip())
        return state, rc

    def test_success_and_notification(self) -> None:
        _, job_dir = self.launch("ok", [sys.executable, "-c", "print('ok')"])
        state, rc = self.wait_terminal(job_dir)
        self.assertEqual((state, rc), ("finished", 0))
        self.assertEqual((job_dir / "notify.status").read_text().strip(), "sent")
        self.assertIn("queue --thread test-thread-id", self.queue_log.read_text())

    def test_command_failure(self) -> None:
        _, job_dir = self.launch("fail", [sys.executable, "-c", "raise SystemExit(7)"])
        state, rc = self.wait_terminal(job_dir)
        self.assertEqual((state, rc), ("failed", 7))

    def test_bad_log_path_still_finalizes(self) -> None:
        bad_log = self.root / "log-is-a-directory"
        bad_log.mkdir()
        _, job_dir = self.launch(
            "bad-log",
            [sys.executable, "-c", "print('never reaches usable log')"],
            extra=["--log", str(bad_log)],
        )
        state, rc = self.wait_terminal(job_dir)
        self.assertEqual(state, "failed")
        self.assertEqual(rc, 74)
        self.assertTrue((job_dir / "COMPLETE").exists())

    def test_cancel(self) -> None:
        info, job_dir = self.launch(
            "cancel",
            [sys.executable, "-c", "import time; time.sleep(60)"],
            extra=["--cancel-grace", "0.2"],
        )
        wait_for(job_dir / "job_pid")
        cp = subprocess.run(
            [str(WRAPPER), "--cancel", info["job_id"], "--state-dir", str(self.state_dir)],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(cp.returncode, 0, msg=cp.stdout + cp.stderr)
        self.assertIn("LONGRUN_CANCEL_REQUESTED", cp.stdout)
        state, rc = self.wait_terminal(job_dir)
        self.assertEqual(state, "cancelled")
        self.assertIn(rc, (130, 143))

    def test_relative_state_dir_is_resolved_from_launch_cwd(self) -> None:
        work = self.root / "work"
        work.mkdir()
        argv = [
            str(WRAPPER),
            "--name", "relative",
            "--state-dir", "relative-state",
            "--workdir", str(work),
            "--",
            sys.executable, "-c", "print('ok')",
        ]
        cp = subprocess.run(argv, cwd=self.root, env=self.env, text=True, capture_output=True, check=False)
        self.assertEqual(cp.returncode, 0, msg=cp.stdout + cp.stderr)
        info = parse_started(cp.stdout)
        job_dir = Path(info["job_dir"])
        self.assertEqual(job_dir.parent, (self.root / "relative-state").resolve())
        state, rc = self.wait_terminal(job_dir)
        self.assertEqual((state, rc), ("finished", 0))


if __name__ == "__main__":
    unittest.main()
