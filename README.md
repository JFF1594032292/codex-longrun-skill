# codex-longrun-skill

[中文说明](README.zh-CN.md)

Run deterministic long-lived commands in Codex **without repeated LLM polling**. The skill detaches the target process, waits outside the model loop, persists terminal state, and wakes the originating Codex thread through `codex queue` when the job reaches a terminal state.

```text
Codex turn
   │
   ├─ launch longrun job
   └─ end turn
          │
          │  no LLM polling
          ▼
   detached worker
          │
          ├─ wait for target process
          ├─ persist state / exit code / logs
          └─ codex queue
                  │
                  ▼
          wake original thread
```

## Why this exists

Long-running tools such as bioinformatics pipelines, builds, large test suites, downloads, model training, and data-processing jobs may run for minutes or hours. An agent that repeatedly checks `ps`, `tail`, output files, or process sessions wastes model turns and context on a deterministic question: *has the process exited yet?*

`codex-longrun-skill` moves that wait into a normal detached OS process. Codex can end the current turn and resume only when the job finishes, fails, or is cancelled.

## Features

- Detached worker based on `subprocess.Popen(..., start_new_session=True)`.
- No model-driven polling while a deterministic job is merely running.
- Completion wake-up through `codex queue` and `CODEX_THREAD_ID`.
- Persistent job metadata, log path, exit code, timestamps, PID/PGID and a `COMPLETE` marker.
- Explicit cancellation with `state=cancelled`.
- Whole-process-group termination: SIGTERM first, then optional SIGKILL escalation after a grace period.
- Worker SIGTERM/SIGINT handling so ordinary signal-based shutdowns still finalize state and attempt notification.
- Notification timeout and bounded retries, with `job_id` for deduplication.
- Absolute path resolution before detach.
- Failure-safe finalization even when the target log itself is unusable.
- Guidance for shell pipelines with `bash -o pipefail`.

## Requirements

- Linux is the primary tested platform.
- Python 3.10+ available as `python3`.
- A Codex CLI environment that exposes `CODEX_THREAD_ID` to launched tools.
- `codex queue --thread ... --message ...` available on `PATH`.

The skill is intentionally a small lifecycle helper, **not** a general-purpose job scheduler or daemon.

## Installation

Clone the repository and copy the skill into your global Codex skills directory:

```bash
git clone https://github.com/JFF1594032292/codex-longrun-skill.git
cd codex-longrun-skill

mkdir -p ~/.codex/skills/longrun
cp -a skill/longrun/. ~/.codex/skills/longrun/
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.sh
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.py
```

Optionally add the guidance in [`docs/AGENTS-global-snippet.md`](docs/AGENTS-global-snippet.md) to your global `AGENTS.md` so Codex more consistently chooses the skill for long deterministic jobs. A Chinese version is available at [`docs/AGENTS-global-snippet.zh-CN.md`](docs/AGENTS-global-snippet.zh-CN.md).

## Quick start

From a Codex-launched shell, run:

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name smoke-test -- \
  bash -c 'echo start; sleep 10; echo done'
```

The launcher returns immediately with output similar to:

```text
LONGRUN_STARTED
job_id=...
watcher_pid=...
job_dir=...
log=...
status=...
cancel=...
```

After launch, Codex should end the current turn rather than polling. When the job reaches a terminal state, the worker attempts to queue a message beginning with:

```text
[AUTOMATED_LONGRUN_COMPLETION]
```

Codex can then inspect the reported state, exit code, log and expected outputs before continuing the original task.

## Cancellation

Use the formal cancellation entry point instead of manually killing the top-level process:

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh --cancel <job_id>
```

For a non-default state directory:

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --cancel <job_id> --state-dir /path/to/state
```

Cancellation is asynchronous. The helper records the request, signals the detached worker, and the worker terminates the target process group. The terminal state is normally:

```text
state=cancelled
exit_code=143
```

If descendants remain after the configured grace period, the target process group is escalated from SIGTERM to SIGKILL.

## Pipelines and compound shell commands

Direct argv execution is preferred. For pipes, redirections, shell variables, or compound shell syntax, invoke Bash explicitly and enable `pipefail`:

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name pipeline -- \
  bash -o pipefail -c 'cmd1 | cmd2 > result.txt'
```

Add `-e` only when the shell workflow intentionally requires immediate termination after an unhandled failing command:

```bash
bash -e -o pipefail -c 'step1; step2 | step3'
```

## Persistent state

By default, job state is stored under:

```text
${CODEX_LONGRUN_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/codex-longrun}/<job_id>/
```

Important files include:

| File | Purpose |
| --- | --- |
| `job.json` | Resolved launch specification and absolute paths |
| `command.sh` | Shell-escaped command representation |
| `job.log` | Combined target stdout/stderr unless `--log` overrides it |
| `wrapper.log` | Detached worker stdout/stderr |
| `state` | `launching`, `running`, `finished`, `failed`, `cancelled`, or `launch_failed` |
| `worker_pid` / `worker_pgid` | Detached worker identity |
| `job_pid` / `job_pgid` | Target process/session identity |
| `exit_code` | Terminal exit status |
| `COMPLETE` | Terminal marker written before notification |
| `notify.status` | `pending`, `sent`, or `failed` |
| `notify.log` | Notification attempt details |

## Notification semantics

Notification is **best effort with bounded retries**, not an exactly-once guarantee. A completion message may arrive once, may be duplicated if a timeout happens after the queue accepted it, or may fail to arrive after all retries.

Use `job_id` as the idempotency key. If a user returns after a missed wake-up, inspect persisted state instead of restarting the job.

## Limitations

- SIGKILL cannot be handled by the worker.
- Pod/host disappearance, machine reboot, storage failure, or abrupt runtime loss can leave stale `running` state.
- `state=running` by itself is therefore not proof that the process is still alive after an infrastructure failure.
- Queue delivery can fail even when the job itself completed successfully.
- The current implementation targets Linux process/session semantics; other operating systems are not the primary supported environment.

## Validation

The implementation has been exercised with successful jobs, command failures, cancellation, SIGTERM handling, child-process-group cleanup, notification retries/timeouts, invalid working directories, invalid log paths, and pipeline failure propagation. It has also been used in a real multi-hour data-processing workflow (about 2 h 40 min) without model polling during the wait.

Run the repository tests with:

```bash
python3 -m unittest discover -s tests -v
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) · [中文](docs/ARCHITECTURE.zh-CN.md)
- [Global AGENTS.md snippet](docs/AGENTS-global-snippet.md) · [中文](docs/AGENTS-global-snippet.zh-CN.md)
- [Contributing](CONTRIBUTING.md) · [中文](CONTRIBUTING.zh-CN.md)
- [Changelog](CHANGELOG.md) · [中文](CHANGELOG.zh-CN.md)

## License

MIT. See [LICENSE](LICENSE).
