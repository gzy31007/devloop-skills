---
name: loop-build
description: 按计划开分支编码：缺陷先写失败回归，跑项目测试与架构门禁，把目标、改动范围、自测命令与打回次数写进 .loop/handoff.md。开始写代码时使用。
---

# loop-build · 写代码

## 一、开分支

从约定的基线拉 `feat/<版本>-<主题>`，一个版本一个提交，父提交写清楚。主干与版本标签由发版流程处理。

## 二、缺陷先红后绿

复现缺陷：先写最小失败用例并确认红灯，再改根因，最后把用例并进回归套件。修完再跑一次，确认同一用例转绿。

## 三、自测

```bash
python -m pytest -q                      # 或项目自带的测试入口
python skills/loop-review/scripts/arch_gate.py --json .   # 架构门禁
```

服务、长命令、占端口的测试走 `reap_and_run.py`，跑完自动回收进程与端口：

```bash
python skills/loop-review/scripts/reap_and_run.py --cwd=. --timeout=600 python verify.py
```

## 四、写交接

`.loop/handoff.md` 按 `references/handoff.template.md` 填：目标、基线、候选 SHA、改动范围、自测命令与退出码、已知待验项、本版打回次数。打回一次就在末尾加一行，写清打回原因与修复方式。

## 五、交给审查

交接写完即停手，按当前 Agent 能力给唯一明确的下一步提示：

- **当前 Agent 支持子代理工具**（如 Antigravity / Claude Code / Codex）：明确告诉用户**直接在当前对话输入 `/loop-review`**（由其自动拉起独立只读子代理盲审），无需另开新对话。
- **当前 Agent 仅支持单会话**：明确提醒用户**新开一个干净对话再输入 `/loop-review`**。
