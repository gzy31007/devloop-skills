---
name: loop-build
description: 按计划开分支编码：对照写审共用清单圈定高风险项并自注入，按四步表格规范返工，跑真实结果与脏输入属性测试、架构门禁与只读 preflight 后提交锁定候选提交。开始编码或返工时使用。
---

# loop-build · 分支编码与交接预检

脚本路径优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。写审同源风险项与故障配方见 `loop-review/references/review-checklist.md`。

## 一、开分支与四步返工

新开版本从约定基线拉出 `feat/<版本>-<主题>` 分支开发，主干合并与打标签交由 `/loop-retro` 处理。动工前先查询 `review-status`；若处于返工状态，先读取最新 `rNN-reject.md` 原件并严格按以下四步执行：

```bash
python <loop-review-dir>/scripts/devloop_ops.py review-status --cwd . --json
```

| 序号 | 返工步骤 | 执行标准 |
| --- | --- | --- |
| 1 | 核验契约与事实 | 对照开工前验收条件与既有行为；若属超出契约且候选零行为退化的过承诺，提供受影响条款、复现证据与基线/候选对比以修订 `DECISIONS.md` 明确接受边界；涉及缩减核心承诺须交人工拍板 |
| 2 | 同类缺陷横扫 | 同步排查兄弟控件、edit/reset 等对称路径与联动视图中的同因缺陷 |
| 3 | 先红后绿修复 | 按复现命令先写最小失败回归用例确认红灯，修复根因后全量转绿并留存退出码 |
| 4 | 预检锁定提交 | 提交后运行 `devloop_ops.py preflight --cwd . --json`，凭新鲜通过输出锁定候选提交 |

编码前按本轮触达领域对照 `loop-review/references/review-checklist.md` 圈定高风险项并落实源头防错；交审前自选 1~2 项真实注入故障并留存新鲜命令输出。

## 二、测试与门禁

| 类别 | 要求 |
| --- | --- |
| 真实结果验证 | 优先验证 CLI 输出、HTTP 响应、真实落盘文件与浏览器交互，拒绝仅 mock 内部函数的凑数单测 |
| 脏输入属性测试 | 涉及解析器、状态机、编解码或规则计算时配属性不变量测试（`hypothesis` / `fast-check`），固定随机种子、打印样本数，覆盖空值、UTF-8 BOM、CRLF、非结构行与边界极值 |

```bash
python <loop-review-dir>/scripts/reap_and_run.py --cwd . --timeout 600 -- <验证命令>
python <loop-review-dir>/scripts/arch_gate.py --json .
```

## 三、文档同步、预检与锁定提交

交审前同步 `DECISIONS.md`、`FEATURE_MAP.md`、清单版本号（`package.json` / `pyproject.toml`），按 `loop-review/references/handoff.template.md` 填齐 `.loop/handoff.md`（纯分支名、完整 `基线 SHA`、契约摘要、改动范围、自测与返工验证证据、风险自检行与两份文档同步声明），再按接入模式提交：

| 接入模式 | 提交范围 | 交接要求 |
| --- | --- | --- |
| 自研（`own`） | 代码、文档、清单版本与 `.loop/handoff.md` 一并提交至当前源分支 | `git status --porcelain` 为空 |
| 客串（`external`） | 仅提交上游候选代码与获准变更，保持上游 `.loop/` 零跟踪 | `git status --porcelain` 为空且 `.loop/` 仅本地忽略 |

提交后运行只读预检，全绿再通知审查；预检失败就地补齐后重新提交并重跑：

```bash
python <loop-review-dir>/scripts/devloop_ops.py preflight --cwd . --json
```

主路径提示用户新开干净会话（推荐跨模型或跨工具）输入 `/loop-review`；支持只读子代理或跨 Agent 协作工具的宿主也可按 `/loop-review` 兜底分支执行。
