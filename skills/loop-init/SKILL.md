---
name: loop-init
description: 为当前仓库幂等接入 devloop 闭环：执行只读预检、写入 .loop/config.json、维护 AGENTS.md 索引块与模板文件，支持自研与客串双模式。首次接入、重复刷新或技术栈变化时使用。
---

# loop-init · 幂等接入闭环

负责仓库初始化与配置基线验证。脚本路径优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与测试夹具优先读取 `DEVLOOP_KIT`。

## 接入模式与文档落点

| 分支 | 条件 | 文档路径 | `AGENTS.md` 处理 | Git 排除 |
| --- | --- | --- | --- | --- |
| O1 自研 | `mode=own` | 根目录 `DECISIONS.md`、`FEATURE_MAP.md` | 创建或原地更新标记块，保留块外字节 | 写 `/{prefix}.loop/reviews/` 与 `/{prefix}.loop/archive/` 至 `info/exclude` 并以 `git check-ignore` 复验（非 Git 目录跳过） |
| E1 客串（未跟踪） | `mode=external` 且 `AGENTS.md` 未受 Git 跟踪 | `.loop/DECISIONS.md`、`.loop/FEATURE_MAP.md` | 创建或原地更新标记块，指向 `.loop/` | 写 `/{prefix}.loop/` 与 `/{prefix}AGENTS.md` 至 `info/exclude` 并以 `git check-ignore` 复验 |
| E2 客串（已跟踪） | `mode=external` 且 `AGENTS.md` 已受上游跟踪 | `.loop/DECISIONS.md`、`.loop/FEATURE_MAP.md` | 保持只读零改动，`.loop/handoff.md` 标为 `handoff-fallback` | 写 `/{prefix}.loop/` 至 `info/exclude` 并以 `git check-ignore` 复验 |

## 执行步骤

运行初始化脚本前自动完成全部只读预检（Git 根、排除文件路径、符号链接、跟踪状态、现有 JSON 与标记块成对合法性），任一异常直接返回退出码 `2` 且零写盘。

```bash
# 自研模式
python <loop-review-dir>/scripts/devloop_ops.py init --cwd . --mode own --json

# 客串模式（须在 Git 仓库内执行；影子配置按规范化仓库路径哈希隔离）
python <loop-review-dir>/scripts/devloop_ops.py init --cwd . --mode external --json
```

重复运行保持幂等：保留已有 `mode` 与其他自定义配置键，`ignore_dirs` 与默认忽略目录去重并集合并，保留 `DECISIONS.md`、`FEATURE_MAP.md` 与 `.loop/handoff.md` 既有内容；如需移除默认忽略目录直接编辑 `.loop/config.json`，显式切换模式传入 `--switch-mode`，迁移目录名影子配置传入 `--migrate-shadow`。

存量仓库刷新 8 技能 `AGENTS.md` 索引块安排在新接入或下一轮 `/loop-plan` 前执行，避开活跃候选过审待发版窗口。

## 基线门禁与交接

初始化完成后运行架构门禁验证基线，按需在 `.loop/config.json` 填写 `generated_paths` 与阈值：

```bash
python <loop-review-dir>/scripts/arch_gate.py --json .
```

确认门禁返回 `PASSED` 且 `.loop/handoff.md` 记录当前模式与基线结果后，进入 `/loop-plan`。
