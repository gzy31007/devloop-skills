---
name: loop-review
description: Breaker 独立盲审：按真实结果优先、寻址键审计、Web 浏览器闭环、脏输入属性检验与文档指纹核验执行只读审查，经 devloop_ops.py review-record 向 .loop/reviews/ 追加不可覆盖裁决原件。交接锁定后使用。
---

# loop-review · 独立只读审查

以只读方式复跑验收并尝试打穿候选提交，仅认现场命令与原始输出。脚本路径优先从本技能目录直接拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`；细则与故障注入配方见 `references/review-checklist.md`。

## 一、写审分离与盲审装包

唯一写审边界：审查期对仓库源码、测试、配置与 `.loop/handoff.md` 严格零改动；裁决输出仅允许经 `devloop_ops.py review-record` 写入 Git 忽略目录 `.loop/reviews/<scope-key>/rNN-<verdict>.md`。

| 当前会话状态 | 执行动作 |
| --- | --- |
| 干净新会话（主路径，推荐跨模型/跨工具） | 在当前会话直接按只读方式审查并调 `review-record` 落盘 |
| 刚参与本轮编码且支持只读子代理或跨 Agent 协作工具（兜底） | 仅向受托方传入客观材料执行只读审查，受托方回传证据与裁决文本，外层会话统一调 `review-record` 落盘 |
| 刚参与本轮编码且无只读委托能力 | 停手并提示用户新开干净对话输入 `/loop-review` |

盲审仅传入客观材料：`.loop/handoff.md`、`git diff <基线>..<候选>`、对应模式的 `FEATURE_MAP.md` 与 `DECISIONS.md`（客串模式在隔离 worktree 审查时通过 `--state-root` 与 `--docs-root` 关联主工作区文档）、`.loop/config.json`、本技能与 `references/review-checklist.md`。

## 二、六步审查流程

| 步骤 | 核心动作 | 阻断条件 |
| --- | --- | --- |
| 1. 范围、契约与文档 | 核对 `git diff --stat` 爆炸半径、寻址键一致性、`FEATURE_MAP.md`（兼容 4 列与 3 列表格）与 `DECISIONS.md` 同步声明 | 越界改动、寻址键混用引发误伤、文档漏更或留空模板占位 |
| 2. 真实结果验收 | 经 `reap_and_run.py` 重跑端到端入口与缺陷回归，排查过度 Mock 或削弱断言 | 命令失败、旧功能退化、凑数单测掩盖核心缺陷 |
| 3. Web 浏览器闭环 | 前端/UI 改动按 `review-checklist.md` 选跑最小基线，核查 5 项标准：控制台报错/白屏、预期外 4xx/5xx、核心交互失效、控件遮挡溢出、脏数据/连点静默丢数据 | 命中 5 项任一条即阻断；无浏览器权限的子代理输出必验清单由外层会话补验；无浏览器环境标 `pending` 待人工验证 |
| 4. 属性不变量检验 | 状态机、解析器、编解码或规则计算重跑属性测试，记录固定种子、样本数与脏输入覆盖 | 核心逻辑缺属性测试、失败或遗漏高危输入域；纯文案/样式注明不适用及理由 |
| 5. 故障注入与门禁 | 参照 `review-checklist.md` 选做 2~4 项坏场景注入；运行 `arch_gate.py --json .`（客串以上游原生检查为准并核查零污染） | 注入打穿未处理、门禁失败或客串模式污染上游工作树 |
| 6. 节奏、熔断与裁决落盘 | 每 3 版核查行为等价的减法重构；核验工作树干净且候选 SHA 等于当前 `HEAD` 后调用 `review-record` 追加落盘 | 工作树脏、忽略规则缺失、文档指纹无效或触发熔断 |

落盘前保持工作树干净（`git status --porcelain` 为空），正文中经 UTF-8 stdin 或仓库外 `--body-file` 传入完整七节裁决单；`pass` 裁决自动内嵌候选 SHA 与两份文档的 SHA-256 指纹并保留全部历史 `rNN-reject.md` 原件（`reject` 满 2 次标记 `cross_model_recommended`，满 3 次标记 `circuit_breaker_triggered`）。

```bash
# 打回返工（必填 --category method|env；跨 worktree 加 --state-root <主工作区> --docs-root <文档根>）
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --verdict reject --category method --reviewer loop-review --json < /tmp/verdict.md

# 全绿放行（自动绑定当前 HEAD 与 DECISIONS/FEATURE_MAP 指纹）
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --verdict pass --reviewer loop-review --json < /tmp/verdict.md
```

## 三、七节裁决单模板

输出与写入 `.loop/reviews/<scope-key>/rNN-<verdict>.md` 正文严格包含 7 节：结论头（`<全绿放行 | 打回返工 | 待人工浏览器验证 | 触发熔断待裁决>`、分支与候选 SHA、审查方式、累计打回次数、减法重构检查点）、1. 真实结果验收与凑数单测排查、2. Web 浏览器验证记录、3. 属性不变量检验（工具/种子/样本数或不适用理由）、4. 故障注入、门禁与文档指纹核验结果、5. 阻断打回清单（附一键复现命令与原始报错，为空则放行）、6. 非阻塞建议、7. 下一步（放行进 `/loop-retro`，打回进 `/loop-build`）。

## 四、附带脚本清单

| 脚本 | 职责 |
| --- | --- |
| `arch_gate.py` / `arch_js.py` | Python 与 TS/JS 行数、复杂度、循环依赖与 `generated_paths` 门禁 |
| `reap_and_run.py` | 跨平台进程树超时与回环端口探测/回收，支持 `--ports` 与 `--status --json` |
| `devloop_ops.py` | 统一 CLI：`resolve-kit`、`init`、`docs-fingerprint`、`review-record`、`review-status`、`review-archive`、`release-gate` |
| `devloop_git.py` / `devloop_markers.py` / `devloop_docs.py` / `devloop_init.py` / `devloop_gate.py` | Git 校验、标记块、文档指纹、幂等初始化、两阶段发版门禁 |
| `devloop_review_io.py` / `devloop_review_store.py` / `devloop_reviews.py` / `devloop_archive.py` | 作用域隔离存储、并发锁与忽略复验、不可覆盖裁决落盘、只读路由状态、整目录归档 |
| `verify_scripts.py` / `verify_ops_cases.py` / `verify_review_cases.py` / `verify_gate_cases.py` | 隔离自测套件，验证脚本、双向交接、门禁、禁语规则与自身架构门禁 |
