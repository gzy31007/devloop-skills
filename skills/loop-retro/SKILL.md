---
name: loop-retro
description: Matt 式 Agent 工作环境复盘与归档收尾：读取指定作用域证据、git 提交、会话摩擦与抽检意见，按四组七维诊断索引输出高低优先级提案，受控落地后原子归档。发版后或排障/单次审查时独立使用。
---

# loop-retro · Agent 工作环境复盘与归档收尾

聚焦改进后续编码与审查所依赖的工作环境。主流程紧接 `/loop-ship` 运行；排障、单次审查或客串模式下亦可随时独立触发。脚本优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。

## 一、加载规范与四路一手输入

编写任何提案、文档或测试标题前先加载并遵循 `agent-docs` 规范。收集以下四路一手输入，禁止拿无关历史归档顶替当前作用域证据：

| 一手输入 | 读取目标与命令 | 核查重点 |
| --- | --- | --- |
| 审查与交接记录 | 显式读取指定 `.loop/reviews/<scope-key>/`（或明确指定要复盘的归档作用域）与 `.loop/handoff.md` | 打回根因（`method`/`env`）、第 6 节 P2/P3、漏接项与 `.ship.json` 绑定 |
| 提交历史 | `git log --oneline -n 20` 及本轮基线至当前范围提交 | 返工震荡热点、跨文件联动漏改、重复修补 |
| 会话摩擦记录 | 当前或指定会话中的报错、工具超时、重跑与卡点 | 缺寻路入口、隐性前置条件、环境噪音与无效回环 |
| 发版抽检意见 | `/loop-ship` 人工抽检反馈、PR 评审意见或人工介入记录 | 标准预期差、文档表达盲区与可回滚风险 |

按上述证据按需查阅仓库 lint/hook/CI 配置、`AGENTS.md`、可选 `CODING_STANDARDS.md`（客串模式 `.loop/CODING_STANDARDS.md`）、`DECISIONS.md` 与 `.loop/config.json`。

## 二、四组七维环境诊断索引

仅围绕手头真实证据展开，禁止逐项空扫套模板；同一根因只定一处精准落点，防止规则多处复制：

| 诊断维度 | 常见信号 | 精准落点方向 |
| --- | --- | --- |
| 导航与指令卫生 | 代理找不到入口、重复查目录、`AGENTS.md` 冗长含无效或过期指令 | 补充精简寻路指针、瘦身并清理失效指令 |
| 自动化门禁与漏接 | 同类格式/静态/路径/端口违规反复靠人工或审查捕获；回归与属性测试缺口 | 机械违规转确定性检查（`arch_gate`/lint/hook），补最小真实回归与脏输入属性用例 |
| 审查标准与写审分流 | 写审对主观风格、降级边界或阻断级别产生临时分歧 | 项目特有主观约定写入 `CODING_STANDARDS.md` 或 `DECISIONS.md`；跨项目通性问题才归入套件建议 |
| 工具经济性与运行期可达性 | 命令反复超时、本地日志或端口状态不可观察导致盲猜 | 补充只读状态命令、清晰错误码或 `reap_and_run.py` 托管脚本入口 |

## 三、分级提案输出与双轨分流

按高、中、低严重度输出结构化提案表，并严格区分项目内改造与套件反哺：

| 输出字段 | 必须包含内容 |
| --- | --- |
| 严重度与标题 | `高` / `中` / `低` 及一句话根因概括 |
| 一手证据 | 具体文件名、提交 SHA、打回轮次或会话报错摘要 |
| 所属轨道 | 「本项目环境改动」或「跨项目 `devloop` 套件反哺建议」 |
| 精准落点与改动内容 | 目标文件路径与具体增删改方案 |

处于业务项目仓库时，「跨项目 `devloop` 套件反哺建议」仅在本报告输出结构化建议块，严禁直接改动全局已安装技能缓存（`~/.agents/skills/`、`~/.opencode/skills/` 等）。

## 四、受控落地与归档收尾

| 运行模式 | 环境改动权限 | 归档前提交要求 |
| --- | --- | --- |
| 交互模式 | 展示提案表后等待用户勾选；仅实施选中项并跑通对应验证；零选中则跳过改动 | 存在实际改动时提交独立 `chore(retro): ...`，记录最新提交作为 `--retro-head` |
| 非交互无人值守模式 | 仅将完整提案报告写入指定日志或标准输出，对仓库源码、配置与文档零改动 | 直接按当前清洁 `HEAD` 进入归档校验 |

仅当存在已完成发版或 PR 交接的活跃作用域（或用户明确确认以 `--close-reason abandoned` 中止未发版作用域）时，才调用 `review-archive`；平时排障或无活跃作用域独立复盘时跳过归档。若主干产生 `chore(retro)` 新提交，归档必须传入 `--retro-head`：

```bash
# 1. 有 retro 提交推进 HEAD 时传 --retro-head；未推进且为 own 标签发版可传 --tag
python <loop-review-dir>/scripts/devloop_ops.py review-archive --cwd . --version vX.Y --branch <branch> --retro-head <cur_head> --json
python <loop-review-dir>/scripts/devloop_ops.py review-archive --cwd . --tag vX.Y --json

# 2. 用户明确中止未发版作用域
python <loop-review-dir>/scripts/devloop_ops.py review-archive --cwd . --version vX.Y --branch <branch> --close-reason abandoned --json

# 3. 终态核验：端口空闲、路由更新、工作树干净
python <loop-review-dir>/scripts/reap_and_run.py --status --json
python <loop-review-dir>/scripts/devloop_ops.py review-status --cwd . --json
git status --porcelain
```
