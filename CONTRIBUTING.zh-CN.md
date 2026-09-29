# 贡献指南

[English](CONTRIBUTING.md)

欢迎贡献。请尽量保持项目轻量、确定性明确：核心目标是让长任务等待脱离模型轮询，而不是发展成完整的作业调度器。

## 开发环境

需要：

- Linux
- Python 3.10+
- Bash

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

语法检查：

```bash
python3 -m py_compile skill/longrun/scripts/run_and_notify.py
bash -n skill/longrun/scripts/run_and_notify.sh
```

## 设计约束

修改时应尽量保持以下性质：

1. 等待确定性目标进程时不应依赖 LLM/model 循环。
2. 应先写入任务终态，再尝试发送通知。
3. 通知失败不得覆盖或改变任务真实退出状态。
4. 取消应针对整个目标进程组，而不是只处理 leader。
5. 辅助日志写入失败不得阻止 finalize。
6. 通知是有限重试的 best-effort 机制，不承诺 exactly-once。
7. 没有明确必要时，不引入 daemon/service 等复杂依赖。

如果修改了外部行为，请增加或更新生命周期测试，并在更新日志中记录用户可见变化。
