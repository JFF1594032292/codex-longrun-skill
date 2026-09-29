# longrun skill

[中文](README.zh-CN.md)

This directory is the installable Codex skill. For the project overview, architecture and public documentation, see the repository [README](../../README.md).

## Install this directory

```bash
mkdir -p ~/.codex/skills/longrun
cp -a skill/longrun/. ~/.codex/skills/longrun/
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.sh
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.py
```

## Smoke test

Run from a Codex-launched shell so `CODEX_THREAD_ID` is available:

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name smoke-test -- \
  bash -c 'echo start; sleep 10; echo done'
```

After `LONGRUN_STARTED`, end the current Codex turn rather than polling. The originating thread should later receive an `[AUTOMATED_LONGRUN_COMPLETION]` event.

## Cancel

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh --cancel <job_id>
```

See [`SKILL.md`](SKILL.md) for the complete agent-facing behavior contract.
