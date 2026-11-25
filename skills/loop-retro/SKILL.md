---
name: loop-retro
description: 审查全绿后执行两阶段发版门禁、合主干打标签、标签复核、分层沉淀复盘教训并清理现场。审查放行后使用。
---

# loop-retro · 发版闸门与复盘反哺

仅在 `/loop-review` 给出「全绿放行」且 `.loop/review-stamp.json` 就绪时执行。脚本优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。

## 一、固定发版顺序与两阶段门禁

受跟踪的 `.loop/handoff.md` 仅记录 `reviewed_sha`（消除自引用 SHA 环）；最终 `release_sha` 记录在提交之外的发版记录或宿主 `telemetry_log`。

| 步骤 | 动作与命令 | 核验目标 |
| --- | --- | --- |
| 1. 对齐版本与文档提交 | 核对 `package.json` / `pyproject.toml` 版本与目标标签一致（兼容 `0.1.0` 对 `v0.1`），核对 `FEATURE_MAP.md`（兼容 4 列与 3 列旧表）与 `DECISIONS.md` 状态，在 `.loop/handoff.md` 追加本轮复盘摘要并记录 `reviewed_sha` 后提交 | 工作树干净，且 `DECISIONS.md` / `FEATURE_MAP.md` 与签章指纹一致 |
| 2. 合前门禁（`pre-merge`） | `python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase pre-merge --tag vX.Y --reviewed-sha <reviewed_sha> --json` | 工作树干净、无 `.tmp/`、端口空闲、`reviewed_sha` 为祖先、差异命中白名单、版本匹配、两份文档指纹匹配且非空模板 |
| 3. 合主干与打标签 | 将候选分支快进或干净合并入主干分支，打标签 `git tag -a vX.Y -m "release vX.Y"` | 主干推进到发版提交，标签创建完成 |
| 4. 标后复核（`post-tag`） | `python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase post-tag --tag vX.Y --branch main --json` | 标签存在、解引用提交与当前主干 `HEAD` 一致且工作树干净 |
| 5. 发版记录与对外推送 | 在提交之外的发版日志记录 `release_sha` 与回滚命令；若配置或宿主声明 `telemetry_log`，追加标准 `released` JSONL 事件；对外推送须遵守用户明确授权 | 遥测与回滚依据完整 |

`telemetry_log` 标准 `released` JSONL 字段：`{"event":"released","tag":"vX.Y","reviewed_sha":"...","release_sha":"...","mode":"own","retro_path":"..."}`。

## 二、分层沉淀复盘教训

按固定优先级沉淀本轮打回与遗留风险，每条教训落入最靠前的一层：

| 优先级 | 落点 | 适用情形 |
| --- | --- | --- |
| 1 | 门禁脚本（`arch_gate.py`、`devloop_ops.py`、`verify_scripts.py`） | 可机械检查并拦截的结构、路径、配置或流程错误 |
| 2 | 行为与脏输入属性回归测试 | 静态检查覆盖不到的运行时缺陷，验证入口同步写入 `FEATURE_MAP.md` |
| 3 | 上游技能（`loop-build`、`loop-review`、`review-checklist.md`、`loop-retro`） | 跨项目通用编码防错、审查盲区或流程改进 |

同步核对 `DECISIONS.md` 现行前提：前提变化则重写条目并将被取代条目标记 `[superseded by YYYY-MM-DD 新摘要]`。

## 三、清理现场

删除临时目录与测试 worktree，校验端口与工作树状态：

```bash
python <loop-review-dir>/scripts/reap_and_run.py --status --json
git status --porcelain
```
