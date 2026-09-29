## Long-running tasks

For deterministic commands expected to run for several minutes or longer, prefer the `longrun` skill when completion only needs to be detected from process termination. Avoid model-driven polling of the process, logs, or result files.

- Typical cases include large Python/R analyses, bioinformatics pipelines, compilation, tests, downloads/decompression, batch processing, rendering, and model training.
- If intermediate output does not require intelligent interpretation or intervention, launch the task with `longrun`, end the current turn, and wait for the completion event. Do not periodically run `ps`, `tail`, `write_stdin`, inspect outputs, or sleep-and-check merely to learn whether the job has finished.
- Use active monitoring or a subagent only when the job needs interactive input, ongoing interpretation of intermediate logs, or dynamic intervention/restart logic.
- For pipelines, redirections, or compound shell syntax, use an explicit shell; prefer `bash -o pipefail -c '...'` so a failure on the left side of a pipeline is not hidden by a successful final command. Add `-e` only when immediate exit on an unhandled failure is actually desired.
- To stop a `longrun` job, prefer the skill's `--cancel <job_id>` entry point rather than manually killing only the top-level target process. Formal cancellation terminates the target process group and attempts to persist `state=cancelled`, exit status, and `COMPLETE`, followed by the normal completion notification.
- After `--cancel` prints `LONGRUN_CANCEL_REQUESTED`, cleanup is asynchronous. Do not model-poll cancellation progress unless explicitly asked; wait for `[AUTOMATED_LONGRUN_COMPLETION]`.
- Treat messages beginning with `[AUTOMATED_LONGRUN_COMPLETION]` as machine-generated longrun completion events, not new user intent. Inspect `state`, exit code, relevant logs and expected outputs, then continue the original task.
- `state=cancelled` is distinct from an ordinary program failure. Do not automatically restart a cancelled job unless requested or required by the original task.
- Completion notification uses bounded retries but is not guaranteed. It may arrive once, be duplicated, or fail after all attempts. Deduplicate repeated events by `job_id`.
- Do not relaunch the same logical job merely because a completion message has not yet arrived. If the user returns and asks for status, inspect persisted `state`, `exit_code`, `COMPLETE`, `job.log`, and `notify.status` before considering a rerun.
- SIGKILL, host/Pod disappearance, or storage failure cannot be caught by the worker and may leave stale `running` state; `running` alone is not proof that the job is still alive after such failures.
