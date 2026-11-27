---
name: loop-review
description: Breaker 独立盲审：第 0 步先跑只读 preflight 拦截交接缺口（零落盘退回），再按 P1/P2 分级、R1 全量与 R2+ 收敛复审、浏览器基线、属性检验与文档纠偏门槛执行审查并经 devloop_ops.py review-record 落盘。交接锁定后使用。
---

# loop-review · 独立只读审查

以只读方式复跑验收并尝试打穿候选提交，仅认现场命令与原始输出。脚本路径优先从本技能目录直接拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`；写审同源考纲见 `references/review-checklist.md`。

## 一、写审分离与盲审装包

唯一写审边界：审查期对仓库源码、测试、配置与 `.loop/handoff.md` 严格零改动；裁决输出仅允许经 `devloop_ops.py review-record` 写入 Git 忽略目录 `.loop/reviews/<scope-key>/rNN-<verdict>.md`。

| 当前会话状态 | 执行动作 |
| --- | --- |
| 干净新会话（主路径，推荐跨模型/跨工具） | 在当前会话直接按只读方式审查并调 `review-record` 落盘 |
| 刚参与本轮编码且支持只读子代理或跨 Agent 协作工具（兜底） | 仅向受托方传入客观材料执行只读审查，受托方回传证据与裁决文本，外层会话统一调 `review-record` 落盘 |
| 刚参与本轮编码且无只读委托能力 | 停手并提示用户新开干净对话输入 `/loop-review` |

盲审仅传入客观材料：`.loop/handoff.md`、`git diff <基线>..<候选>`、对应模式的 `FEATURE_MAP.md` 与 `DECISIONS.md`（若仓库存在可选 `CODING_STANDARDS.md` 或 `.loop/CODING_STANDARDS.md`，一并传入同候选快照；客串模式先固定摘要再纳入考量；隔离 worktree 审查时通过 `--state-root` 与 `--docs-root` 关联主工作区）、`.loop/config.json`、本技能与 `references/review-checklist.md`。

## 二、预检门禁、裁决分级与审查流程

| 步骤 | 核心动作 | 阻断与退出条件 |
| --- | --- | --- |
| 0. 预检与熔断检查 | 运行 `review-status` 与 `preflight`（跨 worktree 补 `--state-root`、`--docs-root`、`--branch`） | 满 3 次打回直接停在人工裁决；`preflight` 失败立即秒退列出修复项指回 `/loop-build`，零写入 `rNN-reject.md`、零计入熔断 |
| 1. 范围与契约对照 | 核对 `preflight` 输出的 `diff_files` 爆炸半径、寻址键一致性、`FEATURE_MAP.md` 与 `DECISIONS.md` 同步声明 | 越界改动、寻址键混用误伤、文档漏更或与实现矛盾 |
| 2. 真实结果验收 | 经 `reap_and_run.py` 重跑端到端入口与缺陷回归，排查过度 Mock 或削弱断言 | 命令失败、旧功能退化、凑数单测掩盖核心缺陷 |
| 3. Web 浏览器闭环 | 按 `review-checklist.md` 核查控制台报错/白屏、预期外 4xx/5xx、对称路径异常、存储拒访/超限回滚与多视图一致 | 命中任一 P1 缺陷即阻断；无浏览器权限由外层补验；无浏览器环境且其余 P1 为空时标 `pending` |
| 4. 属性与故障注入 | 重跑属性测试（固定种子与样本数），并选做 2~4 项高风险故障注入（含契约内延迟/并发调度）；运行 `arch_gate.py --json .` | 缺失或打穿已声明契约、门禁失败或客串模式污染上游工作树 |
| 5. 节奏与裁决落盘 | 每 3 版核查行为等价减法重构；确认工作树洁净后调 `review-record` 追加落盘 | 锁内复验候选 SHA 或文档指纹漂移 |

| 裁决等级 | 适用情形 | 处置 |
| --- | --- | --- |
| P1 阻断 | 核心功能缺陷、数据损毁/静默丢失、未捕获异常、真实故障注入打穿已声明契约或规划期锁定的标准条款、文档/ADR 与实现矛盾、门禁失败、越界改动或削弱测试 | 写入 `rNN-reject.md`（仅缺浏览器环境且其余 P1 为空写 `rNN-pending.md`） |
| P2 / P3 非阻塞建议 | 超契约理论极值、锦上添花加固、未在规划期锁定的主观风格优化 | 写入裁决单第 6 节并放行，留待 `/loop-retro` 评估 |

| 轮次策略 | 审查范围 | 恢复全审条件 |
| --- | --- | --- |
| R1 首轮 | 全量审查，一次性报齐全部 P1 阻断项 | — |
| R2+ 复审 | 先独立快扫整版范围、契约与关键入口，再读取本作用域历史 `rNN-reject.md` 原件逐项复验前轮 P1、兄弟控件、对称路径及 `prev_reviewed_sha..HEAD` 增量；新证实 P1 一律阻断 | 上轮受审 SHA 不可达，或契约/改动范围发生实质变化时恢复全审 |

| Builder ADR 纠偏申请门槛 | 审查标准 |
| --- | --- |
| 放行修订 `DECISIONS.md` | 证据证明场景超出获准契约、候选相对基线零行为退化，且原验收条件与断言完整保留 |
| 维持 P1 交人工裁决 | 缩减已有承诺、支持路径静默丢数据、基线不明或复现证据矛盾 |

```bash
# 第 0 步只读预检（跨 worktree 补 --state-root <主工作区> --docs-root <文档根> --branch <分支>）
python <loop-review-dir>/scripts/devloop_ops.py preflight --cwd . --json

# 打回返工（必填 --category method|env）或全绿放行
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --verdict reject --category method --reviewer loop-review --json < /tmp/verdict.md
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --verdict pass --reviewer loop-review --json < /tmp/verdict.md
```

## 三、七节裁决单模板

输出与写入 `.loop/reviews/<scope-key>/rNN-<verdict>.md` 正文严格包含 7 节：结论头（`<全绿放行 | 打回返工 | 待人工浏览器验证 | 触发熔断待裁决>`、分支与候选 SHA、审查方式、累计打回次数、减法重构检查点）、1. 真实结果验收与凑数单测排查、2. Web 浏览器验证记录、3. 属性不变量检验（工具/种子/样本数或不适用理由）、4. 故障注入、门禁与文档指纹核验结果、5. 阻断打回清单（P1 项与一键复现命令，为空则放行）、6. 非阻塞建议（P2/P3 项）、7. 下一步（放行进 `/loop-ship`，打回进 `/loop-build`）。

## 四、附带脚本清单

| 脚本 | 职责 |
| --- | --- |
| `arch_gate.py` / `arch_js.py` | Python 与 TS/JS 行数、复杂度、循环依赖与 `generated_paths` 门禁 |
| `reap_and_run.py` | 跨平台进程树超时与回环端口探测/回收，支持 `--ports` 与 `--status --json` |
| `devloop_ops.py` | 统一 CLI：`resolve-kit`、`init`、`docs-fingerprint`、`preflight`、`review-record`、`review-status`、`ship-record`、`review-archive`、`release-gate` |
| `devloop_git.py` / `devloop_markers.py` / `devloop_docs.py` / `devloop_init.py` / `devloop_gate.py` / `devloop_ship.py` | Git 校验与差异清单、标记块、文档指纹、幂等初始化、两阶段发版门禁、发版凭据与共同校验 |
| `devloop_preflight.py` / `devloop_status.py` / `devloop_review_io.py` / `devloop_review_store.py` / `devloop_reviews.py` / `devloop_archive.py` | 只读预检、路由状态、作用域存储、并发锁、不可覆盖裁决落盘、抗崩溃整目录归档 |
| `verify_scripts.py` / `verify_ops_cases.py` / `verify_preflight_cases.py` / `verify_review_cases.py` / `verify_gate_cases.py` / `verify_ship_retro_cases.py` | 隔离自测套件，验证预检、双向交接、发版复盘、门禁、禁语规则与自身架构门禁 |
