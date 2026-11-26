---
name: loop-retro
description: 审查全绿后执行两阶段发版门禁、合主干打标签、标签复核、读取全部打回原件分层沉淀教训并原子归档审查目录。审查放行后使用。
---

# loop-retro · 发版闸门、复盘与归档

仅在 `devloop_ops.py review-status` 返回 `latest_verdict == "pass"` 且 `pass_stale == false` 时执行。脚本优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。

## 一、固定发版顺序与两阶段门禁

受跟踪的 `.loop/handoff.md` 为 `pre-merge` 审查后差异唯一白名单；清单版本（`package.json` / `pyproject.toml`）与 `DECISIONS.md`、`FEATURE_MAP.md` 须在审查前随候选提交锁定，最终 `release_sha` 记录在提交之外的发版日志或宿主 `telemetry_log`。

| 步骤 | 动作与命令 | 核验目标 |
| --- | --- | --- |
| 1. 复核与提交交接 | 核对版本标签、`.loop/handoff.md` 与当前提交树一致；如需补充发版前复盘摘要仅修改 `.loop/handoff.md` 并提交 | 工作树干净，未改源码、清单与两份文档 |
| 2. 合前门禁（`pre-merge`） | `python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase pre-merge --tag vX.Y --json` | 工作树干净、无 `.tmp/`、端口空闲、当前作用域最新裁决为 `pass`、差异仅含 `.loop/handoff.md`、版本与文档指纹全匹配 |
| 3. 合主干与打标签 | 切回 `main` 后优先快进合并；若无法快进则完成合并并在合并树重跑测试与 `arch_gate.py --json .` 通过后再执行 `git tag -a vX.Y -m "release vX.Y"` | 主干推进到合并后最新提交，合并树验证通过再打标签 |
| 4. 标后复核（`post-tag`） | `python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase post-tag --tag vX.Y --branch main --json` | 标签存在、解引用提交与当前主干 `HEAD` 一致且工作树干净 |
| 5. 读取打回与归档 | 读取 `.loop/reviews/<scope-key>/` 下全部 `rNN-reject.md` 原件按分层沉淀表落盘，随后执行 `python <loop-review-dir>/scripts/devloop_ops.py review-archive --cwd . --tag vX.Y --json` | 教训落盘；无论打回 0 次或多次，整目录原子移入 `.loop/archive/<scope-key>/` |
| 6. 发版记录与推送 | 在提交之外的发版日志记录 `release_sha` 与回滚命令；若配置或宿主声明 `telemetry_log`，追加标准 `released` JSONL 事件；对外推送须遵守用户明确授权 | 遥测与回滚依据完整 |

`telemetry_log` 标准 `released` JSONL 字段：`{"event":"released","tag":"vX.Y","reviewed_sha":"...","release_sha":"...","mode":"own","retro_path":"..."}`。

## 二、分层沉淀复盘教训

按固定优先级沉淀本轮 `.loop/reviews/<scope-key>/` 中全部打回原件与遗留风险，项目特化规则留在本仓库 `DECISIONS.md` / `AGENTS.md`，跨项目通用模式才回流套件共用清单或门禁脚本：

| 优先级 | 落点 | 适用情形 |
| --- | --- | --- |
| 1 | 门禁脚本（`arch_gate.py`、`devloop_ops.py`、`verify_scripts.py`） | 可机械检查并拦截的跨项目结构、路径、配置或流程错误 |
| 2 | 本项目测试与本仓库文档（`DECISIONS.md`、`FEATURE_MAP.md`、`AGENTS.md`） | 单项目专属运行时回归、领域不变量与项目特化工程约定 |
| 3 | 套件共用清单与技能（`review-checklist.md`、`loop-build`、`loop-review`、`loop-retro`） | 跨项目通性编码防错、写审同源核对项或流程改进 |

同步核对 `DECISIONS.md` 现行前提：前提变化则重写条目并将被取代条目标记 `[superseded by YYYY-MM-DD 新摘要]`。

## 三、清理现场

删除临时目录与测试 worktree，校验端口、工作树与待归档队列均为空：

```bash
python <loop-review-dir>/scripts/reap_and_run.py --status --json
python <loop-review-dir>/scripts/devloop_ops.py review-status --cwd . --json
git status --porcelain
```
