---
name: loop-review
description: Breaker 独立盲审：按真实结果优先、寻址键审计、Web 浏览器闭环、脏输入属性检验与文档同步指纹签章执行只读审查，输出七节裁决单并附带门禁脚本。交接完成后使用。
---

# loop-review · 独立只读审查

以只读方式复跑验收并尝试打穿候选提交，仅认现场命令与原始输出。脚本路径优先从本技能目录直接拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`；细则与故障注入配方见 `references/review-checklist.md`。

## 一、写审单次隔离与盲审装包

| 当前会话状态 | 执行动作 |
| --- | --- |
| 刚参与本轮编码且支持子代理 | 派发 1 个只读隔离子代理盲审 |
| 刚参与本轮编码且仅支持单会话 | 停手并提示用户新开干净对话输入 `/loop-review` |
| 干净新会话（未改文件） | 在当前会话直接按只读方式审查 |

盲审仅传入客观材料：`.loop/handoff.md`、`git diff <基线>..<候选>`、对应模式的 `FEATURE_MAP.md` 与 `DECISIONS.md`（客串模式在隔离 worktree 审查时附只读快照及基线 SHA）、`.loop/config.json`、本技能与 `references/review-checklist.md`。

## 二、六步审查流程

| 步骤 | 核心动作 | 阻断条件 |
| --- | --- | --- |
| 1. 范围、契约与文档 | 核对 `git diff --stat` 爆炸半径、寻址键一致性、`FEATURE_MAP.md`（兼容 4 列与 3 列表格）与 `DECISIONS.md` 同步声明 | 越界改动、寻址键混用引发误伤、文档漏更或留空模板占位 |
| 2. 真实结果验收 | 经 `reap_and_run.py` 重跑端到端入口与缺陷回归，排查过度 Mock 或削弱断言 | 命令失败、旧功能退化、凑数单测掩盖核心缺陷 |
| 3. Web 浏览器闭环 | 前端/UI 改动按 `review-checklist.md` 选跑最小基线，核查 5 项标准：控制台报错/白屏、预期外 4xx/5xx、核心交互失效、控件遮挡溢出、脏数据/连点静默丢数据 | 命中 5 项任一条即阻断；无浏览器权限的子代理输出必验清单由主会话补验；无浏览器环境标「待人工浏览器验证」 |
| 4. 属性不变量检验 | 状态机、解析器、编解码或规则计算重跑属性测试，记录固定种子、样本数与脏输入覆盖 | 核心逻辑缺属性测试、失败或遗漏高危输入域；纯文案/样式注明不适用及理由 |
| 5. 故障注入与门禁 | 参照 `review-checklist.md` 选做 2~4 项坏场景注入；运行 `arch_gate.py --json .`（客串以上游原生检查为准并核查零污染） | 注入打穿未处理、门禁失败或客串模式污染上游工作树 |
| 6. 节奏、熔断与指纹签章 | 每 3 版核查行为等价的减法重构；累计有效打回满 2 次标记跨模型抽检，满 3 次熔断待裁决；放行前生成文档同步指纹签章 | 阻断项非空、签章校验失败或触发熔断 |

全绿放行前执行签章命令，绑定候选 SHA 与两份文档的 SHA-256 指纹：

```bash
python <loop-review-dir>/scripts/devloop_ops.py review-stamp --cwd . --reviewed-sha <candidate_sha> --json
```

## 三、七节裁决单模板

输出严格包含 7 节：结论头（`<全绿放行 | 打回返工 | 待人工浏览器验证 | 触发熔断待裁决>`、分支与候选 SHA、审查方式、累计打回次数、减法重构检查点）、1. 真实结果验收与凑数单测排查、2. Web 浏览器验证记录、3. 属性不变量检验（工具/种子/样本数或不适用理由）、4. 故障注入、门禁与文档指纹签章结果、5. 阻断打回清单（附一键复现命令与原始报错，为空则放行）、6. 非阻塞建议、7. 下一步（放行进 `/loop-retro`，打回进 `/loop-build`）。

## 四、附带脚本清单

| 脚本 | 职责 |
| --- | --- |
| `arch_gate.py` / `arch_js.py` | Python 与 TS/JS 行数、复杂度、循环依赖与 `generated_paths` 门禁 |
| `reap_and_run.py` | 跨平台进程树超时与回环端口探测/回收，支持 `--ports` 与 `--status --json` |
| `devloop_ops.py` | 统一 CLI：`resolve-kit`、`init`、`docs-fingerprint`、`review-stamp`、`release-gate` |
| `devloop_git.py` / `devloop_markers.py` / `devloop_docs.py` / `devloop_init.py` / `devloop_gate.py` | 标准库职责拆分模块（Git 校验、标记块、指纹签章、初始化、两阶段发版门禁） |
| `verify_scripts.py` / `verify_ops_cases.py` / `verify_gate_cases.py` | 隔离自测套件，验证脚本、门禁、禁语规则与自身架构门禁 |
