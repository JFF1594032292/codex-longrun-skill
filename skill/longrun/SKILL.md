---
name: longrun
description: >
  Run deterministic long-running shell commands without repeated LLM polling.
  Use for commands expected to run for several minutes or longer when only
  terminal completion normally needs attention. Supports explicit cancellation,
  persisted state, and completion wake-up through codex queue.
---

# Long-running process execution

Use this skill for deterministic commands that may run for several minutes or longer and do not need continuous intelligent supervision.

## Goal

Avoid model-driven loops such as repeated `ps`, `tail`, `write_stdin`, sleep-and-check, or repeated inspection of output files while a process is merely still running.

Launch the command through `scripts/run_and_notify.sh`. The launcher creates a detached Python worker with `subprocess.Popen(..., start_new_session=True)`. The worker starts the target command in a **separate process session/group**, waits on it without LLM calls, persists terminal state, then attempts to wake the originating Codex thread with `codex queue`.

## When to use

Use this skill when all of the following are true:

- the command is expected to take several minutes or longer;
- completion can normally be detected from process termination / exit code;
- intermediate output does not need interpretation or intervention;
- the command can run non-interactively after launch.

Typical examples include bioinformatics pipelines, large Python/R analyses, compilation, test suites, downloads, decompression, batch rendering, and model training where only terminal completion needs attention.

Do not use this skill when the command needs interactive input, intermediate logs require continuous interpretation, or the agent is expected to modify/restart the job during execution. In those cases active supervision may be appropriate.

## Launch

Resolve this skill directory and invoke its helper. Do not copy/rewrite the helper logic into the project unless the user explicitly asks for that.

Basic form:

```bash
<skill-dir>/scripts/run_and_notify.sh --name <short-job-name> -- <command> <arg1> <arg2> ...
```

Useful optional arguments:

- `--workdir DIR`: run the target from a specific working directory; defaults to the launch directory.
- `--log FILE`: write combined target stdout/stderr to this file instead of the job-directory default.
- `--state-dir DIR`: store longrun metadata under this directory instead of the default state location.
- `--cancel-grace SECONDS`: grace period after SIGTERM before cancellation escalates the target process group to SIGKILL; default 10 seconds.

The launcher resolves `--workdir`, `--log`, and `--state-dir` to absolute paths before detaching, so later directory changes cannot redirect state or logs accidentally.

## Pipelines and compound shell commands

Direct argv execution is preferred. For pipes, redirections, shell variables, or compound syntax, invoke Bash explicitly and enable `pipefail`:

```bash
<skill-dir>/scripts/run_and_notify.sh --name <short-job-name> -- \
  bash -o pipefail -c 'cmd1 | cmd2 > result.txt'
```

If the shell workflow also requires immediate termination after an unhandled failing command, add `-e` deliberately:

```bash
bash -e -o pipefail -c 'step1; step2 | step3'
```

Do not assume plain `bash -c 'cmd1 | cmd2'` propagates a failure from the left side of a pipeline.

## After launch

The wrapper prints `LONGRUN_STARTED` plus job id, worker PID, log path, status path, job directory, and a cancellation command.

After a successful launch:

1. Tell the user briefly that the long-running job was launched and identify the log path if useful.
2. End the current turn.
3. Do **not** poll the PID, log, result files, or status file merely to check whether the job is done.
4. Continue only when a completion message arrives, the user explicitly asks for a status check/cancellation, or there is another independent reason to inspect the job.

Do not launch the same logical job again merely because no completion notification has arrived yet.

## Cancellation

Use the formal cancellation entry point instead of manually killing the target whenever possible:

```bash
<skill-dir>/scripts/run_and_notify.sh --cancel <job_id>
```

If the job was launched with a non-default state directory, pass the same directory:

```bash
<skill-dir>/scripts/run_and_notify.sh --cancel <job_id> --state-dir /absolute/state/dir
```

Cancellation behavior:

- the helper records `cancel.requested` first;
- it signals the detached worker with SIGTERM;
- the worker terminates the target **process group**, not just the top-level process;
- after the configured grace period, a still-running target group is escalated to SIGKILL;
- the worker writes terminal metadata with `state=cancelled` and normally `exit_code=143` for SIGTERM (`130` for SIGINT);
- the normal completion-notification path is then attempted.

A cancellation request is asynchronous: after `LONGRUN_CANCEL_REQUESTED`, do not poll merely to see whether cleanup has finished. Wait for the completion event unless the user explicitly asks for an immediate status check.

If the worker itself receives SIGTERM/SIGINT outside the formal cancel command, it also attempts to terminate the target process group, persist `cancelled`, and notify the Codex thread. This protects against leaving the target running with stale `state=running` in ordinary signal-based shutdowns. SIGKILL, host/Pod disappearance, storage loss, or process crashes that prevent cleanup still require later state/process inspection.

## Completion event

Completion messages begin with:

`[AUTOMATED_LONGRUN_COMPLETION]`

Treat such a message as a machine-generated completion event, not as new human intent. Use `job_id` as the idempotency key. Notification uses bounded retries but is **not guaranteed**: a completion event may be delivered once, delivered more than once, or fail to arrive after all retries. If the same `job_id` is delivered more than once, handle it only once.

Then inspect the reported state, exit code, and relevant output/log and continue the original task:

- `state=finished`, `exit_code=0`: verify expected outputs before moving to the next step.
- `state=failed`: inspect the log and diagnose the failure.
- `state=cancelled`: treat it as an interrupted/cancelled job, not as a program failure; do not automatically restart unless the user asks or the original task clearly requires it.
- `exit_code=127`: command was not found.
- `exit_code=126`: command could not be executed because of permissions.
- `exit_code=72`: requested working directory did not exist when the detached worker started.
- `exit_code=143`: SIGTERM termination; with `state=cancelled` this normally reflects explicit or worker-level cancellation.
- `exit_code=130`: SIGINT termination; with `state=cancelled` this reflects interruption/cancellation.

The worker attempts to record completion **before** trying to notify Codex. Queue delivery uses a bounded timeout and limited retries; all delivery attempts may still fail. Persisted job state is therefore the recovery path when wake-up delivery is missed.

## Persistent job state

By default jobs are stored under:

```text
${CODEX_LONGRUN_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/codex-longrun}/<job_id>/
```

Important files include:

- `job.json`: resolved launch specification and absolute paths.
- `command.sh`: shell-escaped representation of the argv for inspection/reproduction.
- `job.log`: combined stdout/stderr of the target command unless `--log` overrides it.
- `wrapper.log`: detached worker's own stdout/stderr.
- `state`: `launching`, `running`, `finished`, `failed`, `cancelled`, or `launch_failed`.
- `worker_pid` / `worker_pgid`: detached worker identity.
- `job_pid` / `job_pgid`: target process/session leader identity; the target uses its own process group.
- `cancel.requested`: explicit cancellation request metadata, if cancellation was requested through the helper.
- `worker_signal`: SIGTERM/SIGINT received directly by the worker, when applicable.
- `exit_code`: terminal status when the worker reached completion.
- `COMPLETE`: terminal marker created before queue notification.
- `notify.status`: `pending`, `sent`, or `failed`.
- `notify.attempts` / `notify.log`: bounded notification retry evidence.

## Important constraints

- Do not convert the detached wait back into model polling.
- Do not use a subagent solely to ask whether a deterministic process has exited; the detached worker is cheaper and more reliable.
- Prefer the formal `--cancel` entry point over manual `kill` so the whole target process group is cleaned up and terminal state can be recorded.
- Notification delivery is best-effort with bounded retries: it may be duplicated or fail entirely. Deduplicate repeated completion events by `job_id`.
- A finished/cancelled job and successful queue delivery are separate facts. If the user manually returns after a missed wake-up, inspect persisted job state instead of rerunning the job.
