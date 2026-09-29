# Changelog

[中文](CHANGELOG.zh-CN.md)

All notable public changes to this project are documented here.

## [0.1.0] - 2026-09-29

Initial public release.

### Added

- Detached long-running command execution without LLM polling.
- Completion wake-up through `codex queue`.
- Persistent state, exit codes, logs and terminal `COMPLETE` marker.
- Formal cancellation with `state=cancelled`.
- Target process-group cleanup with SIGTERM → SIGKILL escalation.
- Worker SIGTERM/SIGINT cleanup path.
- Notification timeout, bounded retries and `job_id` deduplication guidance.
- Absolute path normalization before detach.
- Failure-safe terminal finalization when target logging is unavailable.
- Pipeline guidance using `bash -o pipefail`.
- Automated lifecycle tests and GitHub Actions workflow.
