---
name: loop-review
description: Breaker 独立盲审：第 0 步先跑只读 preflight 与熔断检查，第 1~4 步执行单一审查者全维通扫与分层止损，第 5 步按 P1-1..N 聚合编号并绑定 --reviewed-sha 快照单次落盘。交接锁定后使用。
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

盲审仅传入客观材料：`.loop/handoff.md`、`git diff <基线>..<候选>`、对应模式的 `FEATURE_MAP.md` 与 `DECISIONS.md`（若仓库存在可选 `CODING_STANDARDS.md` 或 `.loop/CODING_STANDARDS.md`，一并传入同候选快照；客串模式在第 0 步同步固定可选 `CODING_STANDARDS.md` 与生效影子配置摘要后再纳入考量；隔离 worktree 审查时通过 `--state-root` 与 `--docs-root` 关联主工作区）、`.loop/config.json`、本技能与 `references/review-checklist.md`。

## 二、第 0 步预检与全维通扫流程

第 0 步「预检与熔断（唯一早退点）」：运行 `review-status` 与 `preflight`（跨 worktree 补 `--state-root`、`--docs-root`、`--branch`）。满 3 次打回直接停在人工裁决；`preflight` 失败立即秒退列出修复项指回 `/loop-build`（零写入 `rNN-reject.md`、零计入熔断）。

通过第 0 步后，记录本次 `preflight` 返回的 `candidate_sha`、`handoff_sha256`、`doc_fingerprints`、`diff_files` 与最新轮号（客串模式同步记录忽略目录中可选 `CODING_STANDARDS.md` 与生效影子配置摘要）。第 1~4 步执行单一只读审查者全维通扫：命中普通 P1 仅登记至待汇总清单，继续完成剩余步骤与全部 `diff_files`。

| 步骤 | 核心动作 | P1 登记重点 |
| --- | --- | --- |
| 1. 范围、契约与 Diff 全量审读 | 逐条目核查 `preflight` 返回的 `diff_files`：重命名/复制（`R*`/`C*`）核对 `old_path -> path` 两端，删除（`D`）核对基线移除影响，修改/新增核对爆炸半径、寻址键一致性、`FEATURE_MAP.md` 与 `DECISIONS.md` 同步声明 | 越界改动、寻址键混用误伤、漏删残留、文档漏更或与实现矛盾 |
| 2. 真实结果与单测排查 | 经 `reap_and_run.py` 重跑端到端入口与缺陷回归，排查过度 Mock、弱断言与削弱原测试 | 命令失败、旧功能退化、凑数单测或削弱断言掩盖缺陷 |
| 3. Web 浏览器闭环 | 按 `review-checklist.md` 核查控制台报错/白屏、预期外 4xx/5xx、对称路径、存储拒访/超限回滚与多视图一致；子代理缺浏览器权限时由外层对同一 `candidate_sha` 补验浏览器后再汇总落盘一次；无浏览器环境且已存在任意 P1 时直写 `rNN-reject.md` 并标 `PENDING_ENV(无浏览器环境)`（`N/A` 仅限无 Web 场景），受运行阻塞标 `BLOCKED_BY:<P1-x>`，仅其余适用检查全绿且零 P1、唯一缺口为无浏览器时写 `rNN-pending.md` | 首屏/控制台报错、预期外 4xx/5xx、连点误删、存储静默丢数、多视图失步 |
| 4. 属性、故障注入与架构门禁 | 重跑固定种子属性测试并选做 2~4 项高风险故障注入；自研模式运行 `arch_gate.py --json .`，客串模式按上游原生检查与适用影子配置执行门禁并核对上游工作树洁净 | 属性缺失或打穿已声明契约、静态门禁失败、客串污染上游工作树 |
| 5. 汇总裁决与快照绑定落盘 | 每 3 版核查行为等价减法重构；全部适用维度收齐后按根因聚合去重并顺次编号 `P1-1..N`；核对覆盖状态表无漏维、工作树洁净、最新轮号未变更且 `handoff`/文档与第 0 步指纹一致；由单一写入方显式传入 `--reviewed-sha <第0步候选SHA>`（及同次 `mode`/`branch`/`state-root`/`docs-root`）调用一次 `review-record` 落盘，漂移即作废旧证据重检 | 漏维未覆盖、工作树非洁净、候选或文档/交接快照漂移 |

### 分层止损矩阵

| 情形 | 早停范围 | 必须完成的兜底检查 |
| --- | --- | --- |
| 普通 P1 | 零早停 | 覆盖表记 `FAIL(P1-x)`，跑完全部适用步骤与全部 `diff_files` |
| 物理运行阻塞（`BLOCKED_BY:<P1-x>`） | 仅跳过直接依赖该运行态的具体下游检查 | 写清失败命令、退出码与直接依赖；独立的 `diff_files` 静态审读、单测质量排查、契约/文档核对与静态门禁照常完成 |
| 架构根基错误（`SKIPPED_ARCH:<P1-x>`） | 仅跳过必换模块中随重写失效的细查 | 引用获准契约条款、列明必换模块与证据；全量 `diff_files` 仍须逐项粗读以暴露其余独立缺陷，文档与静态门禁照常执行 |

### 裁决分级、R1/R2+ 轮次策略与 ADR 纠偏

`rNN` 为递增落盘序号（`reject`、`pending` 与 `pass` 均顺序占号）。P1 门槛保持从严，严禁为显全面将 P2/P3、超契约假想或主观风格升格为 P1；同根因引发的多处表现合并为一条 `P1-k` 并列全影响位置。

| 裁决等级 | 适用情形 | 处置 |
| --- | --- | --- |
| P1 阻断 | 核心功能缺陷、数据损毁/静默丢失、未捕获异常、真实故障注入打穿已声明契约或规划期锁定的标准条款、文档/ADR 与实现矛盾、门禁失败、越界改动或削弱测试 | 写入 `rNN-reject.md`（仅其余适用检查全绿零 P1 且唯一缺口为无浏览器环境时写 `rNN-pending.md`） |
| P2 / P3 非阻塞建议 | 超契约理论极值、锦上添花加固、未在规划期锁定的主观风格优化 | 写入裁决单第 6 节并放行，留待 `/loop-retro` 评估 |

| 轮次策略 | 审查范围 | 恢复全审条件 |
| --- | --- | --- |
| R1 首轮 | 全维通扫全部 `diff_files` 与第 1~4 步，按分层止损一次性报齐全部 `P1-1..N` 与 P2/P3 | — |
| R2+ 复审 | 禁止在首个未闭项或新缺陷处早停；区分增量锚点（最近一份证据可复用记录的 `reviewed_sha`）与返工待销项来源（最近一份仍有未闭 P1 的 `reject`，历史无编号原件按第 5 节条目顺次映射为 `rNN:#1..#k`）。先独立快扫整版范围、契约与关键入口，再逐条复验全部待销项、兄弟控件、对称路径、`prev_reviewed_sha..HEAD` 增量，以及前轮 `BLOCKED_BY`/`SKIPPED_ARCH`/`PENDING_ENV` 中本轮已解除阻塞或须重验的维度；覆盖表明确区分本轮执行与 `REUSED(rNN)`，未闭项与新增项重新顺次编号，延续项标 `(延续 rNN:P1-x)` | 锚点缺失、非当前候选祖先、范围或契约实质变化、或历史覆盖无法确认 |

| Builder ADR 纠偏申请门槛 | 审查标准 |
| --- | --- |
| 放行修订 `DECISIONS.md` | 证据证明场景超出获准契约、候选相对基线零行为退化，且原验收条件与断言完整保留 |
| 维持 P1 交人工裁决 | 缩减已有承诺、支持路径静默丢数据、基线不明或复现证据矛盾 |

```bash
# 第 0 步只读预检（跨 worktree 补 --state-root <主工作区> --docs-root <文档根> --branch <分支>）
python <loop-review-dir>/scripts/devloop_ops.py preflight --cwd . --json

# 汇总后绑定第 0 步 candidate_sha 单次落盘（reject / pending / pass）
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --reviewed-sha <candidate_sha> --verdict reject --category method --reviewer loop-review --json < /tmp/verdict.md
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --reviewed-sha <candidate_sha> --verdict pending --reviewer loop-review --json < /tmp/verdict.md
python <loop-review-dir>/scripts/devloop_ops.py review-record --cwd . --reviewed-sha <candidate_sha> --verdict pass --reviewer loop-review --json < /tmp/verdict.md
```

## 三、七节裁决单模板

写入 `.loop/reviews/<scope-key>/rNN-<verdict>.md` 的正文严格保留以下结论头与 1~7 节编号：

- **结论头**：结论（`全绿放行 | 打回返工 | 待人工浏览器验证 | 触发熔断待裁决`）、分支与候选 SHA、审查方式、累计打回次数、减法重构检查点；紧接 5 行紧凑**审查覆盖表**（列为 `维度 | 状态 | 证据索引`，允许同行组合多态如 `PASS(...); FAIL(P1-2); BLOCKED_BY:P1-1`）、`diff_files` 统计（总条目 / 已粗读 / 已完成细查，含重命名两端与删除核对）及 R2+ 前轮 P1 复验汇总。

| 覆盖维度 | 可选状态示例 | 证据索引 |
| --- | --- | --- |
| `1. 范围/契约与 Diff 审读` | `PASS` / `FAIL(P1-x)` / `REUSED(rNN)` | `diff_files` 逐条记录与文档同步核对 |
| `2. 真实结果与单测排查` | `PASS` / `FAIL(P1-x)` / `BLOCKED_BY:P1-x` / `SKIPPED_ARCH:P1-x` / `REUSED(rNN)` | 命令、退出码与单测排查结论 |
| `3. Web 浏览器闭环` | `PASS` / `FAIL(P1-x)` / `PENDING_ENV(无浏览器环境)` / `BLOCKED_BY:P1-x` / `N/A(纯后端/CLI)` / `REUSED(rNN)` | 浏览器运行证据或无 Web 适用说明 |
| `4. 属性与故障注入` | `PASS` / `FAIL(P1-x)` / `BLOCKED_BY:P1-x` / `SKIPPED_ARCH:P1-x` / `N/A` / `REUSED(rNN)` | 随机种子、样本数、故障场景与命令结果 |
| `5. 架构门禁与工作树洁净` | `PASS` / `FAIL(P1-x)` | `arch_gate.py` / 原生门禁与 `git status` 输出 |

- **1. 真实结果验收与凑数单测排查** 至 **4. 故障注入、门禁与文档指纹核验结果**：按第 1~4 步逐节填写命令、退出码、随机种子/样本数、门禁数值与指纹核验。
- **5. 阻断打回清单**：每条使用固定编号 `P1-1..N`，按根因聚合，写明来源维度、位置/入口、复现命令或静态证据、违背条款与修复目标（触发止损时写清直接阻塞或必换模块范围）；为空则放行。
- **6. 非阻塞建议**：P2/P3 非阻塞优化建议。
- **7. 下一步**：放行进 `/loop-ship`；打回进 `/loop-build`；`pending` 标明同 SHA 同输入由人工补验后重新复审，若改代码、交接或文档须回 `/loop-build` 锁定新提交。

## 四、附带脚本清单

| 脚本 | 职责 |
| --- | --- |
| `arch_gate.py` / `arch_js.py` | Python 与 TS/JS 行数、复杂度、循环依赖与 `generated_paths` 门禁 |
| `reap_and_run.py` | 跨平台进程树超时与回环端口探测/回收，支持 `--ports` 与 `--status --json` |
| `devloop_ops.py` | 统一 CLI：`resolve-kit`、`init`、`docs-fingerprint`、`preflight`、`review-record`、`review-status`、`ship-record`、`review-archive`、`release-gate` |
| `devloop_git.py` / `devloop_markers.py` / `devloop_docs.py` / `devloop_init.py` / `devloop_gate.py` / `devloop_ship.py` | Git 校验与差异清单、标记块、文档指纹、幂等初始化、两阶段发版门禁、发版凭据与共同校验 |
| `devloop_preflight.py` / `devloop_status.py` / `devloop_review_io.py` / `devloop_review_store.py` / `devloop_reviews.py` / `devloop_archive.py` | 只读预检、路由状态、作用域存储、并发锁、不可覆盖裁决落盘、抗崩溃整目录归档 |
| `verify_scripts.py` / `verify_ops_cases.py` / `verify_preflight_cases.py` / `verify_review_cases.py` / `verify_gate_cases.py` / `verify_ship_retro_cases.py` | 隔离自测套件，验证预检、双向交接、发版复盘、门禁、禁语规则与自身架构门禁 |
