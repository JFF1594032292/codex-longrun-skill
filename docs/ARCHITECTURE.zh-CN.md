# 架构说明

[English](ARCHITECTURE.md)

## 执行模型

项目把三个生命周期拆开：

```text
Codex 模型 turn
      │
      └─ launcher
            │  Popen(start_new_session=True)
            ▼
       独立 worker
            │
            └─ 目标命令
               （独立 session / process group）
```

launcher 只负责环境检查、绝对路径解析、创建 job 目录和 specification，并启动独立 worker，随后立即返回。

真正的长时间等待由 worker 完成。worker 再把目标程序放进独立的 process session/group，通过普通 OS 进程等待机制阻塞；目标结束后先写入终态，再尝试通过 `codex queue` 发送完成消息。

所以目标程序即使运行两小时，也不需要模型连续产生两小时的轮询 turn。

## 为什么 worker 和目标任务要分成两个独立 session？

worker 首先要脱离 Codex tool-call 的进程生命周期；目标任务又要放在另一个独立 session/process group 中。这样取消任务时可以只结束目标进程树，而不会把负责写入终态和发送通知的 worker 一起杀掉。

## 终态写入顺序

预期顺序是：

```text
目标程序终止
    ↓
确定 state + exit code
    ↓
写 exit_code / finished_at / state / COMPLETE
    ↓
尝试 codex queue 通知
```

通知故意放在持久化之后。即使 queue 投递失败，任务终态仍然可以从磁盘恢复。

## 取消机制

正式取消首先写入 `cancel.requested`，再给独立 worker 发送信号。worker 对目标进程组发送 SIGTERM，等待可配置的宽限时间；若整个 PGID 仍存在，再对该进程组发送 SIGKILL。随后把任务写成 `cancelled` 并走正常完成通知流程。

Linux 环境下，在给记录的 worker PID 发信号之前，会尽量通过 `/proc/<pid>/cmdline` 核对它是否仍然是该 job 的 worker，从而降低 PID 被复用后误杀无关进程的风险。

## 故障边界

设计上明确区分三件事：

- 目标程序本身的运行结果；
- 终态是否成功持久化；
- 完成消息是否成功投递。

目标任务可以成功结束但通知失败；目标日志损坏也不应该阻止终态写入或通知尝试。

SIGKILL、主机/Pod 直接消失、存储不可用等无法捕获的基础设施级故障仍可能留下 stale state，需要后续人工或其他机制核对。
