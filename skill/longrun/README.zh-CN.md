# longrun skill

[English](README.md)

这个目录就是可以直接安装的 Codex Skill。项目整体介绍、架构与公开文档见仓库根目录的[中文 README](../../README.zh-CN.md)。

## 安装

```bash
mkdir -p ~/.codex/skills/longrun
cp -a skill/longrun/. ~/.codex/skills/longrun/
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.sh
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.py
```

## Smoke test

需要从 Codex 启动的 shell 中运行，以便环境里存在 `CODEX_THREAD_ID`：

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name smoke-test -- \
  bash -c 'echo start; sleep 10; echo done'
```

看到 `LONGRUN_STARTED` 后就结束当前 Codex turn，不要继续轮询。之后原 thread 应收到 `[AUTOMATED_LONGRUN_COMPLETION]` 事件。

## 取消

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh --cancel <job_id>
```

完整的 agent 行为规范见 [`SKILL.md`](SKILL.md)。
