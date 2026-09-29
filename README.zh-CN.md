# codex-longrun-skill

[English](README.md)

让 Codex 在执行确定性的长时间任务时，**不再依赖 LLM 反复轮询**。该 Skill 会把目标进程放到独立的后台进程会话中，在模型循环之外等待；任务进入终态后持久化状态，并通过 `codex queue` 唤醒原来的 Codex thread。

```text
Codex 当前 turn
   │
   ├─ 启动 longrun 任务
   └─ 结束当前 turn
          │
          │  等待期间不调用 LLM
          ▼
      独立 worker
          │
          ├─ 等待目标进程
          ├─ 写入状态 / 退出码 / 日志
          └─ codex queue
                  │
                  ▼
          唤醒原 Codex thread
```

## 为什么需要它

生物信息学流程、编译、大型测试、下载、模型训练和数据处理等任务经常要运行数分钟甚至数小时。如果 agent 不断执行 `ps`、`tail`、检查输出文件或 session，只为了确认“程序结束了吗”，就会把大量模型 turn 和上下文浪费在一个完全确定的问题上。

`codex-longrun-skill` 把等待工作交给普通的操作系统进程。Codex 启动任务后即可结束当前 turn，只在任务完成、失败或被取消时恢复工作。

## 功能

- 使用 `subprocess.Popen(..., start_new_session=True)` 创建真正独立的后台 worker。
- 确定性任务运行期间无需模型驱动的轮询。
- 通过 `CODEX_THREAD_ID` + `codex queue` 唤醒原 thread。
- 持久化 job 元数据、日志路径、退出码、时间戳、PID/PGID 和 `COMPLETE` 标记。
- 正式的取消入口和独立的 `state=cancelled` 状态。
- 取消整个目标进程组：先 SIGTERM，超过宽限时间后可升级 SIGKILL。
- worker 自身收到 SIGTERM/SIGINT 时会尝试清理目标任务、写入终态并发送通知。
- 通知有超时和有限重试，并使用 `job_id` 去重。
- detach 前统一解析绝对路径。
- 即使目标日志路径本身异常，也不会阻止终态写入与通知尝试。
- 对 shell 管道明确推荐 `bash -o pipefail`。

## 要求

- 主要测试平台为 Linux。
- Python 3.10+，并可通过 `python3` 调用。
- Codex CLI 启动的工具环境中能够读取 `CODEX_THREAD_ID`。
- `PATH` 中存在 `codex queue --thread ... --message ...`。

它只是一个很小的任务生命周期辅助工具，**不是**通用作业调度器或守护进程。

## 安装

克隆仓库并复制到全局 Codex skills 目录：

```bash
git clone https://github.com/JFF1594032292/codex-longrun-skill.git
cd codex-longrun-skill

mkdir -p ~/.codex/skills/longrun
cp -a skill/longrun/. ~/.codex/skills/longrun/
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.sh
chmod +x ~/.codex/skills/longrun/scripts/run_and_notify.py
```

建议把 [`docs/AGENTS-global-snippet.zh-CN.md`](docs/AGENTS-global-snippet.zh-CN.md) 中的规则按需加入全局 `AGENTS.md`，让 Codex 更稳定地在长时间确定性任务中选择这个 Skill。英文版见 [`docs/AGENTS-global-snippet.md`](docs/AGENTS-global-snippet.md)。

## 快速测试

在 Codex 启动的 shell 环境中执行：

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name smoke-test -- \
  bash -c 'echo start; sleep 10; echo done'
```

launcher 应当立即返回：

```text
LONGRUN_STARTED
job_id=...
watcher_pid=...
job_dir=...
log=...
status=...
cancel=...
```

此时 Codex 应结束当前 turn，而不是继续轮询。任务进入终态后，worker 会尝试向原 thread 投递以下开头的消息：

```text
[AUTOMATED_LONGRUN_COMPLETION]
```

Codex 随后检查状态、退出码、日志和预期输出，再继续原任务。

## 取消任务

优先使用正式取消入口，不要手工只杀掉顶层目标进程：

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh --cancel <job_id>
```

如果启动时使用了非默认状态目录：

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --cancel <job_id> --state-dir /path/to/state
```

取消是异步的。helper 先记录请求并给独立 worker 发送信号；worker 再终止整个目标进程组。常见终态是：

```text
state=cancelled
exit_code=143
```

若宽限时间结束后仍有后代进程存活，会把整个目标进程组从 SIGTERM 升级为 SIGKILL。

## 管道和复合 shell 命令

优先直接传 argv。涉及管道、重定向、shell 变量或复合语法时，显式调用 Bash，并启用 `pipefail`：

```bash
~/.codex/skills/longrun/scripts/run_and_notify.sh \
  --name pipeline -- \
  bash -o pipefail -c 'cmd1 | cmd2 > result.txt'
```

只有确实希望“任一未处理失败立即退出”时，再额外加 `-e`：

```bash
bash -e -o pipefail -c 'step1; step2 | step3'
```

## 持久化状态

默认保存在：

```text
${CODEX_LONGRUN_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/codex-longrun}/<job_id>/
```

主要文件：

| 文件 | 用途 |
| --- | --- |
| `job.json` | 已解析的启动参数与绝对路径 |
| `command.sh` | shell 转义后的命令表示 |
| `job.log` | 默认目标 stdout/stderr 合并日志 |
| `wrapper.log` | detached worker 自身日志 |
| `state` | `launching`、`running`、`finished`、`failed`、`cancelled` 或 `launch_failed` |
| `worker_pid` / `worker_pgid` | 独立 worker 标识 |
| `job_pid` / `job_pgid` | 目标任务进程/会话标识 |
| `exit_code` | 终态退出码 |
| `COMPLETE` | 通知前写入的终态标记 |
| `notify.status` | `pending`、`sent` 或 `failed` |
| `notify.log` | 通知尝试详情 |

## 通知语义

通知是**有限重试的 best-effort 机制**，不是 exactly-once 保证。消息可能正常收到一次；也可能因为 queue 已经接收但客户端超时而重试，导致重复；还可能所有尝试均失败。

因此应把 `job_id` 当作幂等键。若没有收到自动唤醒，而用户之后回来询问，应检查持久化状态，而不是直接重新运行任务。

## 限制

- worker 无法捕获 SIGKILL。
- Pod/主机直接消失、机器重启、存储异常或运行时突然终止，仍可能留下陈旧的 `running` 状态。
- 所以基础设施异常之后，单纯看到 `state=running` 并不能证明任务仍然存活。
- job 成功结束和通知成功投递是两件独立的事。
- 当前实现主要面向 Linux 的 process/session 语义，其他系统不是主要支持目标。

## 验证情况

目前已经实际验证：正常完成、命令报错、取消、SIGTERM、子进程组清理、通知重试/超时、错误工作目录、错误日志路径和 pipeline 失败传播。它也已用于真实的多小时数据处理流程（约 2 小时 40 分钟），等待期间无需模型轮询。

运行仓库测试：

```bash
python3 -m unittest discover -s tests -v
```

## 文档

- [架构说明](docs/ARCHITECTURE.zh-CN.md) · [English](docs/ARCHITECTURE.md)
- [全局 AGENTS.md 示例](docs/AGENTS-global-snippet.zh-CN.md) · [English](docs/AGENTS-global-snippet.md)
- [贡献指南](CONTRIBUTING.zh-CN.md) · [English](CONTRIBUTING.md)
- [更新日志](CHANGELOG.zh-CN.md) · [English](CHANGELOG.md)

## License

MIT，见 [LICENSE](LICENSE)。
