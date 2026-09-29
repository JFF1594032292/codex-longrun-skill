# Contributing

[中文](CONTRIBUTING.zh-CN.md)

Contributions are welcome. Keep the project small and deterministic: the core goal is to remove model polling from long-running commands, not to become a full scheduler.

## Development

Requirements:

- Linux
- Python 3.10+
- Bash

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

Run a syntax check:

```bash
python3 -m py_compile skill/longrun/scripts/run_and_notify.py
bash -n skill/longrun/scripts/run_and_notify.sh
```

## Design expectations

Changes should preserve these properties:

1. Waiting for a deterministic target process must not require an LLM/model loop.
2. Terminal state should be persisted before notification is attempted.
3. Notification failure must not erase or rewrite the job's actual exit status.
4. Cancellation should target the target process group, not only its leader.
5. Auxiliary logging failures should not prevent finalization.
6. Notification is best effort with bounded retries, not exactly-once delivery.
7. Avoid adding daemon/service dependencies unless there is a clear need.

For behavior changes, add or update a lifecycle test and document user-visible changes in the changelog.
