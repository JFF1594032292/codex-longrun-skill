# 更新日志

[English](CHANGELOG.md)

这里记录项目公开版本的重要变化。

## [0.1.0] - 2026-09-29

首次公开发布。

### 新增

- 不依赖 LLM 轮询的 detached 长任务执行机制。
- 通过 `codex queue` 在终态后唤醒原 Codex thread。
- 持久化状态、退出码、日志和 `COMPLETE` 终态标记。
- 正式取消入口和 `state=cancelled`。
- 目标进程组整体清理，支持 SIGTERM → SIGKILL 升级。
- worker 对 SIGTERM/SIGINT 的清理流程。
- 通知超时、有限重试以及基于 `job_id` 的去重建议。
- detach 前绝对路径规范化。
- 目标日志不可用时仍可完成终态记录与通知尝试。
- `bash -o pipefail` 管道使用说明。
- 自动化生命周期测试和 GitHub Actions 工作流。
