# Architecture

[中文](ARCHITECTURE.zh-CN.md)

## Execution model

The project separates three lifetimes:

```text
Codex model turn
      │
      └─ launcher
            │  Popen(start_new_session=True)
            ▼
       detached worker
            │
            └─ target command
                 (own session/process group)
```

The launcher only validates the environment, resolves absolute paths, creates the job directory/specification and starts the detached worker. It then returns immediately.

The worker owns the long wait. It launches the target in a separate process session/group, blocks on normal OS process primitives, persists terminal state, and only then attempts a `codex queue` completion message.

This means a two-hour target does not require a two-hour sequence of model turns.

## Why two separate process sessions?

The worker is detached from the Codex tool-call process lifetime. The target is then placed in another independent session/process group so cancellation can terminate the target tree without terminating the worker responsible for recording terminal state and sending the completion event.

## Terminal-state ordering

The intended ordering is:

```text
target terminates
    ↓
determine state + exit code
    ↓
write exit_code / finished_at / state / COMPLETE
    ↓
attempt codex queue notification
```

Notification is deliberately downstream of persistence. If queue delivery fails, the completed job still has a durable recovery record.

## Cancellation

Formal cancellation writes `cancel.requested` and signals the detached worker. The worker sends SIGTERM to the target process group, waits for a configurable grace period, then sends SIGKILL to the group if it is still present. The worker finalizes the job as `cancelled` and attempts the normal completion notification.

The cancellation path checks the recorded worker PID against Linux `/proc/<pid>/cmdline` when available before signalling it, reducing the risk of signalling an unrelated process after PID reuse.

## Failure boundaries

The design intentionally treats these as separate facts:

- target process outcome;
- terminal-state persistence;
- completion-message delivery.

A target may finish successfully even if notification fails. Likewise, a broken target log should not stop terminal state from being persisted or a completion message from being attempted.

Uncatchable or infrastructure-level failures such as SIGKILL, host/Pod loss, or storage disappearance can still leave stale state and require later reconciliation.
