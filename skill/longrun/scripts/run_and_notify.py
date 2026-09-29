#!/usr/bin/env python3
"""Run deterministic long commands detached from a Codex tool call.

The launcher returns immediately. A detached worker waits on the target OS
process without LLM polling, persists terminal state, and attempts to wake the
originating Codex thread with ``codex queue``.

The worker supports explicit cancellation and signal-aware cleanup. The target
command is placed in its own process session/group, so cancellation can
terminate the whole target process tree without signalling the detached worker
itself.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from typing import Any

NOTIFY_TIMEOUT_SECONDS = 20
NOTIFY_DELAYS_SECONDS = (0, 2, 5)
CANCEL_GRACE_SECONDS = 10.0
SPEC_VERSION = 4
TERMINAL_STATES = {"finished", "failed", "cancelled", "launch_failed"}


class WorkerSignal(BaseException):
    """Interrupt the blocking target wait when the detached worker is signalled."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def safe_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value.strip())
    cleaned = cleaned.strip("._-")
    return cleaned[:80] or "job"


def absolute_from(base: pathlib.Path, value: str) -> pathlib.Path:
    p = pathlib.Path(os.path.expandvars(os.path.expanduser(value)))
    if not p.is_absolute():
        p = base / p
    return p.resolve(strict=False)


def default_state_dir(base: pathlib.Path) -> pathlib.Path:
    override = os.environ.get("CODEX_LONGRUN_DIR")
    if override:
        return absolute_from(base, override)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return absolute_from(base, os.path.join(xdg, "codex-longrun"))
    home = os.environ.get("HOME")
    if home:
        return absolute_from(base, os.path.join(home, ".local", "state", "codex-longrun"))
    return pathlib.Path("/tmp/codex-longrun").resolve()


def write_command_file(job_dir: pathlib.Path, command: list[str]) -> None:
    rendered = " ".join(shlex.quote(part) for part in command)
    atomic_write(job_dir / "command.sh", rendered + "\n")


def append_log(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if text and not text.endswith("\n"):
            fh.write("\n")


def best_effort_atomic_write(path: pathlib.Path, text: str) -> bool:
    try:
        atomic_write(path, text)
        return True
    except Exception:
        return False


def best_effort_append(path: pathlib.Path, text: str) -> bool:
    try:
        append_log(path, text)
        return True
    except Exception:
        return False


def internal_note(job_dir: pathlib.Path, text: str) -> None:
    best_effort_append(job_dir / "worker_internal.log", f"[{utc_now()}] {text}")


def read_text(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception:
        return None


def read_int(path: pathlib.Path) -> int | None:
    text = read_text(path)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def send_completion_notification(spec: dict[str, Any], rc: int, state: str, finished_at: str) -> None:
    job_dir = pathlib.Path(spec["job_dir"])
    notify_log = job_dir / "notify.log"
    best_effort_atomic_write(job_dir / "notify.status", "pending\n")
    best_effort_atomic_write(job_dir / "notify.attempts", "0\n")
    best_effort_atomic_write(notify_log, "")

    message = "\n".join(
        [
            "[AUTOMATED_LONGRUN_COMPLETION]",
            "This is a machine-generated completion event from the longrun skill, not a human instruction.",
            f"job_id={spec['job_id']}",
            f"job_name={spec['job_name']}",
            f"state={state}",
            f"exit_code={rc}",
            f"job_dir={spec['job_dir']}",
            f"log={spec['log_file']}",
            f"finished_at={finished_at}",
            "Delivery uses bounded retries and is not guaranteed: messages may be duplicated or all attempts may fail.",
            "If the same job_id is seen again, handle it only once.",
            "If state=cancelled, treat it as an interrupted/cancelled job rather than a program failure.",
            "Inspect the exit status and relevant outputs/log, then continue the original task.",
            "Do not treat this event as new user intent.",
        ]
    )

    for attempt, delay in enumerate(NOTIFY_DELAYS_SECONDS, start=1):
        if delay:
            time.sleep(delay)
        best_effort_atomic_write(job_dir / "notify.attempts", f"{attempt}\n")
        best_effort_append(notify_log, f"[{utc_now()}] attempt={attempt}")
        try:
            cp = subprocess.run(
                [
                    spec["codex_bin"],
                    "queue",
                    "--thread",
                    spec["thread_id"],
                    "--message",
                    message,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=NOTIFY_TIMEOUT_SECONDS,
                check=False,
            )
            if cp.stdout:
                best_effort_append(notify_log, cp.stdout)
            best_effort_append(notify_log, f"returncode={cp.returncode}")
            if cp.returncode == 0:
                best_effort_atomic_write(job_dir / "notify.status", "sent\n")
                best_effort_atomic_write(job_dir / "notified_at", utc_now() + "\n")
                return
        except subprocess.TimeoutExpired as exc:
            best_effort_append(notify_log, f"timeout_after={NOTIFY_TIMEOUT_SECONDS}s")
            if exc.stdout:
                data = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
                best_effort_append(notify_log, data)
        except Exception as exc:
            best_effort_append(notify_log, f"notification_exception={type(exc).__name__}: {exc}")

    best_effort_atomic_write(job_dir / "notify.status", "failed\n")


def finalize(spec: dict[str, Any], rc: int, state: str, detail: str | None = None) -> None:
    """Persist terminal state first, then attempt wake-up.

    The target log and notification bookkeeping are intentionally not control-flow
    dependencies. A bad log path must not leave a job stuck in ``running``.
    """
    job_dir = pathlib.Path(spec["job_dir"])
    log_file = pathlib.Path(spec["log_file"])
    finished = utc_now()

    if detail:
        best_effort_atomic_write(job_dir / "worker_error", detail + "\n")
        if not best_effort_append(log_file, f"[longrun] {detail}"):
            internal_note(job_dir, f"unable to append terminal detail to target log {log_file}: {detail}")

    records = {
        "exit_code": f"{rc}\n",
        "finished_at": finished + "\n",
        "state": state + "\n",
        "COMPLETE": "",
    }
    for name, value in records.items():
        if not best_effort_atomic_write(job_dir / name, value):
            internal_note(job_dir, f"failed to write terminal metadata file: {name}")

    try:
        send_completion_notification(spec, rc=rc, state=state, finished_at=finished)
    except Exception as exc:
        internal_note(job_dir, f"completion notification path raised {type(exc).__name__}: {exc}")


def install_worker_signal_handlers() -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def handler(signum: int, _frame: Any) -> None:
        raise WorkerSignal(signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, handler)
    return previous


def ignore_worker_signals() -> None:
    """Prevent a second TERM/INT from interrupting terminal cleanup/notification."""
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, signal.SIG_IGN)
        except Exception:
            pass


def worker_pid_matches(pid: int, spec_path: pathlib.Path) -> bool | None:
    """Best-effort guard against signalling a PID reused by an unrelated process.

    On Linux, verify the live process command line still identifies this job's
    detached worker. Return None when /proc-style verification is unavailable.
    """
    cmdline_path = pathlib.Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.exists():
        return None
    try:
        parts = [part.decode(errors="replace") for part in cmdline_path.read_bytes().split(b"\0") if part]
    except Exception:
        return None
    expected_spec = str(spec_path.resolve(strict=False))
    return "__worker" in parts and expected_spec in parts


def terminate_target_group(proc: subprocess.Popen[Any], job_dir: pathlib.Path, grace_seconds: float) -> None:
    """Terminate the whole target process group, escalating after a grace period.

    Do not use only the session leader's exit as proof that cleanup is complete:
    descendants can remain in the same process group after the leader exits.
    """
    pgid = proc.pid  # target is launched with start_new_session=True, so pgid == pid
    if not pgid_alive(pgid):
        return

    try:
        os.killpg(pgid, signal.SIGTERM)
        internal_note(job_dir, f"sent SIGTERM to target process group pgid={pgid}")
    except ProcessLookupError:
        return
    except Exception as exc:
        internal_note(job_dir, f"failed to SIGTERM target process group pgid={pgid}: {type(exc).__name__}: {exc}")

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while pgid_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.1)

    if pgid_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
            internal_note(job_dir, f"escalated to SIGKILL for target process group pgid={pgid}")
        except ProcessLookupError:
            pass
        except Exception as exc:
            internal_note(job_dir, f"failed to SIGKILL target process group pgid={pgid}: {type(exc).__name__}: {exc}")

    # Reap the target leader if it is our child. Descendant zombies may be reaped by
    # their new parent after the worker exits; they no longer execute user code.
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def worker(spec_path: pathlib.Path) -> int:
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"longrun worker: unable to read spec: {exc}", file=sys.stderr)
        return 70

    job_dir = pathlib.Path(spec["job_dir"])
    log_file = pathlib.Path(spec["log_file"])
    target_proc: subprocess.Popen[Any] | None = None
    install_worker_signal_handlers()

    try:
        atomic_write(job_dir / "worker_pid", f"{os.getpid()}\n")
        atomic_write(job_dir / "worker_pgid", f"{os.getpgrp()}\n")
        atomic_write(job_dir / "started_at", utc_now() + "\n")
        atomic_write(job_dir / "state", "running\n")
        if not best_effort_append(log_file, f"[longrun] started job_id={spec['job_id']} at {utc_now()}"):
            internal_note(job_dir, f"unable to append start marker to target log: {log_file}")

        workdir = pathlib.Path(spec["workdir"])
        if not workdir.is_dir():
            ignore_worker_signals()
            finalize(spec, rc=72, state="failed", detail=f"working directory does not exist: {workdir}")
            return 0

        command = list(spec["command"])
        try:
            with log_file.open("a", encoding="utf-8") as log_fh:
                target_proc = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=os.environ.copy(),
                    start_new_session=True,
                    close_fds=True,
                )
                best_effort_atomic_write(job_dir / "job_pid", f"{target_proc.pid}\n")
                best_effort_atomic_write(job_dir / "job_pgid", f"{target_proc.pid}\n")
                raw_rc = target_proc.wait()

            ignore_worker_signals()
            rc = 128 + abs(raw_rc) if raw_rc < 0 else raw_rc
            cancel_requested = (job_dir / "cancel.requested").exists()
            if cancel_requested:
                state = "cancelled"
            else:
                state = "finished" if rc == 0 else "failed"
            finalize(spec, rc=rc, state=state)
            return 0

        except WorkerSignal as event:
            # Do not allow another TERM/INT to interrupt cleanup/finalization.
            ignore_worker_signals()
            signame = signal.Signals(event.signum).name
            best_effort_atomic_write(job_dir / "worker_signal", f"{event.signum} {signame}\n")
            if target_proc is not None:
                terminate_target_group(target_proc, job_dir, float(spec.get("cancel_grace_seconds", CANCEL_GRACE_SECONDS)))
            rc = 128 + event.signum
            detail = f"worker received {signame}; target process group was cancelled if still running"
            finalize(spec, rc=rc, state="cancelled", detail=detail)
            return 0
        except FileNotFoundError as exc:
            ignore_worker_signals()
            finalize(spec, rc=127, state="failed", detail=f"command not found: {exc.filename or command[0]}")
            return 0
        except PermissionError as exc:
            ignore_worker_signals()
            finalize(spec, rc=126, state="failed", detail=f"permission denied: {exc.filename or command[0]}")
            return 0
        except OSError as exc:
            ignore_worker_signals()
            finalize(spec, rc=74, state="failed", detail=f"job log or process I/O error: {type(exc).__name__}: {exc}")
            return 0
        except Exception as exc:
            ignore_worker_signals()
            finalize(spec, rc=1, state="failed", detail=f"command execution error: {type(exc).__name__}: {exc}")
            return 0

    except WorkerSignal as event:
        ignore_worker_signals()
        signame = signal.Signals(event.signum).name
        best_effort_atomic_write(job_dir / "worker_signal", f"{event.signum} {signame}\n")
        if target_proc is not None:
            terminate_target_group(target_proc, job_dir, float(spec.get("cancel_grace_seconds", CANCEL_GRACE_SECONDS)))
        finalize(spec, rc=128 + event.signum, state="cancelled", detail=f"worker received {signame} during startup/cleanup")
        return 0
    except Exception as exc:
        ignore_worker_signals()
        try:
            if target_proc is not None and target_proc.poll() is None:
                terminate_target_group(target_proc, job_dir, float(spec.get("cancel_grace_seconds", CANCEL_GRACE_SECONDS)))
            finalize(spec, rc=70, state="failed", detail=f"worker internal error: {type(exc).__name__}: {exc}")
            return 0
        except Exception as final_exc:
            print(f"longrun worker fatal error: {exc}; finalize also failed: {final_exc}", file=sys.stderr)
            return 70


def build_launch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a long command detached and wake the current Codex thread when it terminates.",
        epilog=(
            "For pipelines/redirections, use an explicit shell with pipefail, e.g. "
            "-- bash -o pipefail -c 'cmd1 | cmd2 > result.txt'"
        ),
    )
    parser.add_argument("--name", default="job", help="Short human-readable job name")
    parser.add_argument("--workdir", default=None, help="Working directory; default is current directory")
    parser.add_argument("--log", default=None, help="Combined stdout/stderr log path")
    parser.add_argument("--state-dir", default=None, help="Base directory for persistent job metadata")
    parser.add_argument("--cancel-grace", type=float, default=CANCEL_GRACE_SECONDS, help="Seconds before cancellation escalates SIGTERM to SIGKILL")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    return parser


def launch(argv: list[str]) -> int:
    parser = build_launch_parser()
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command supplied; put the command after --")
    if args.cancel_grace < 0:
        parser.error("--cancel-grace must be >= 0")

    thread_id = os.environ.get("CODEX_THREAD_ID", "").strip()
    if not thread_id:
        print("ERROR: CODEX_THREAD_ID is not set; cannot wake the originating Codex thread.", file=sys.stderr)
        return 69

    codex_bin = shutil.which("codex")
    if not codex_bin:
        print("ERROR: 'codex' is not available on PATH.", file=sys.stderr)
        return 69
    codex_bin = str(pathlib.Path(codex_bin).resolve(strict=False))

    launch_cwd = pathlib.Path.cwd().resolve(strict=False)
    workdir = absolute_from(launch_cwd, args.workdir) if args.workdir else launch_cwd
    state_dir = absolute_from(launch_cwd, args.state_dir) if args.state_dir else default_state_dir(launch_cwd)

    job_name = args.name.strip() or "job"
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    job_id = f"{stamp}_{safe_name(job_name)}_{uuid.uuid4().hex[:8]}"
    job_dir = (state_dir / job_id).resolve(strict=False)

    try:
        job_dir.mkdir(parents=True, exist_ok=False)
    except Exception as exc:
        print(f"ERROR: cannot create state directory {job_dir}: {exc}", file=sys.stderr)
        return 73

    if args.log:
        log_file = absolute_from(launch_cwd, args.log)
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            atomic_write(job_dir / "state", "launch_failed\n")
            atomic_write(job_dir / "launcher_error", f"cannot create log directory: {exc}\n")
            print(f"ERROR: cannot create log directory for {log_file}: {exc}", file=sys.stderr)
            return 73
    else:
        log_file = job_dir / "job.log"

    spec = {
        "version": SPEC_VERSION,
        "job_id": job_id,
        "job_name": job_name,
        "thread_id": thread_id,
        "codex_bin": codex_bin,
        "launch_cwd": str(launch_cwd),
        "workdir": str(workdir),
        "state_dir": str(state_dir),
        "job_dir": str(job_dir),
        "log_file": str(log_file),
        "command": command,
        "cancel_grace_seconds": args.cancel_grace,
        "created_at": utc_now(),
    }

    spec_path = job_dir / "job.json"
    atomic_write(spec_path, json.dumps(spec, ensure_ascii=False, indent=2) + "\n")
    atomic_write(job_dir / "thread_id", thread_id + "\n")
    atomic_write(job_dir / "job_name", job_name + "\n")
    atomic_write(job_dir / "workdir", str(workdir) + "\n")
    atomic_write(job_dir / "log_path", str(log_file) + "\n")
    atomic_write(job_dir / "state", "launching\n")
    write_command_file(job_dir, command)

    wrapper_log = job_dir / "wrapper.log"
    try:
        with wrapper_log.open("ab", buffering=0) as wrapper_fh:
            proc = subprocess.Popen(
                [sys.executable, str(pathlib.Path(__file__).resolve()), "__worker", str(spec_path)],
                stdin=subprocess.DEVNULL,
                stdout=wrapper_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                cwd=str(launch_cwd),
                env=os.environ.copy(),
            )
    except Exception as exc:
        atomic_write(job_dir / "state", "launch_failed\n")
        atomic_write(job_dir / "launcher_error", f"{type(exc).__name__}: {exc}\n")
        print(f"ERROR: failed to start detached worker: {exc}", file=sys.stderr)
        return 70

    atomic_write(job_dir / "watcher_pid", f"{proc.pid}\n")

    print("LONGRUN_STARTED")
    print(f"job_id={job_id}")
    print(f"watcher_pid={proc.pid}")
    print(f"job_dir={job_dir}")
    print(f"log={log_file}")
    print(f"status={job_dir / 'state'}")
    print(f"cancel={pathlib.Path(__file__).with_name('run_and_notify.sh').resolve()} --cancel {shlex.quote(job_id)} --state-dir {shlex.quote(str(state_dir))}")
    return 0


def build_cancel_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Request cancellation of an active longrun job.")
    parser.add_argument("job_id", help="Job id printed by LONGRUN_STARTED")
    parser.add_argument("--state-dir", default=None, help="Base state directory if non-default was used at launch")
    return parser


def cancel_job(argv: list[str]) -> int:
    parser = build_cancel_parser()
    args = parser.parse_args(argv)
    base = pathlib.Path.cwd().resolve(strict=False)
    state_dir = absolute_from(base, args.state_dir) if args.state_dir else default_state_dir(base)
    job_dir = (state_dir / args.job_id).resolve(strict=False)

    if not job_dir.is_dir():
        print(f"ERROR: job not found: {job_dir}", file=sys.stderr)
        return 66

    state = read_text(job_dir / "state") or "unknown"
    if state in TERMINAL_STATES or (job_dir / "COMPLETE").exists():
        print("LONGRUN_ALREADY_TERMINAL")
        print(f"job_id={args.job_id}")
        print(f"state={state}")
        rc = read_text(job_dir / "exit_code")
        if rc is not None:
            print(f"exit_code={rc}")
        return 0

    requested_at = utc_now()
    request = {
        "requested_at": requested_at,
        "requested_by_pid": os.getpid(),
        "signal": "SIGTERM",
    }
    try:
        atomic_write(job_dir / "cancel.requested", json.dumps(request, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:
        print(f"ERROR: cannot record cancellation request: {exc}", file=sys.stderr)
        return 73

    worker_pid = read_int(job_dir / "worker_pid") or read_int(job_dir / "watcher_pid")
    if worker_pid is None:
        print("ERROR: no worker PID is recorded; cancellation request was saved but no signal was sent.", file=sys.stderr)
        return 70

    spec_path = job_dir / "job.json"
    if not pid_alive(worker_pid):
        print("ERROR: worker is no longer running; cancellation request was recorded but no signal was sent. Inspect persisted job state/processes.", file=sys.stderr)
        return 70

    identity = worker_pid_matches(worker_pid, spec_path)
    if identity is False:
        print(
            f"ERROR: pid={worker_pid} is alive but no longer matches this longrun worker; refusing to signal a possibly reused PID.",
            file=sys.stderr,
        )
        return 70

    try:
        os.kill(worker_pid, signal.SIGTERM)
    except ProcessLookupError:
        print("ERROR: worker exited before it could be signalled; cancellation request remains recorded. Inspect persisted job state.", file=sys.stderr)
        return 70
    except PermissionError as exc:
        print(f"ERROR: permission denied signalling worker pid={worker_pid}: {exc}", file=sys.stderr)
        return 77

    print("LONGRUN_CANCEL_REQUESTED")
    print(f"job_id={args.job_id}")
    print(f"worker_pid={worker_pid}")
    print(f"requested_at={requested_at}")
    print("The worker will terminate the target process group, finalize state=cancelled, and attempt the normal completion notification.")
    return 0


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "__worker":
        return worker(pathlib.Path(sys.argv[2]))
    if len(sys.argv) >= 2 and sys.argv[1] == "--cancel":
        return cancel_job(sys.argv[2:])
    return launch(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
