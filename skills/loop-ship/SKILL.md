---
name: loop-ship
description: 审查放行后输出精简三块人工抽检摘要，按自研与客串双模式执行两阶段发版门禁、快进合主干打标签（或 PR 级交接）并写入 .ship.json 完成凭据。审查全绿后使用。
---

# loop-ship · 发版摘要、机械闸门与交接凭据

仅在 `devloop_ops.py review-status` 返回 `recommended_next == "/loop-ship"`（即 `latest_verdict == "pass"` 且 `pass_stale == false`）时执行。脚本优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。

## 一、人工抽检摘要（零增仓库文件）

执行发版动作前先输出以下三块紧凑摘要，并在标题显式绑定 `(version, branch, reviewed_sha, target)`；本步严禁新增或修改仓库内任何文件。

| 摘要块 | 呈现要求 | 内容来源 |
| --- | --- | --- |
| `Summary` | 按改动形态选用一种最小视觉表达（文件树、调用树、组件树、伪代码或紧凑 diff 简图） | `.loop/handoff.md` 与 `git diff --stat <baseline>..<reviewed_sha>` |
| `Evidence` | 直接引用命令、退出码、关键结果，以及第 6 节保留的 P2/P3 建议 | `.loop/reviews/<scope-key>/rNN-pass.md` |
| `Merge Danger` | 标明 `one-way` 或 `two-way` 门型、影响面与一条可执行回滚命令 | `DECISIONS.md`、数据库/配置迁移与外发影响评估 |

交互模式输出上述摘要后等待用户快速抽检确认，继续执行前复核 `HEAD == reviewed_sha`；已获明确发版授权或非交互自动化运行时，输出上述摘要后直接进入第 2 步。

## 二、双模式发版流水线

`own` 模式中 `.loop/handoff.md` 为审查后差异唯一白名单，清单版本与 `DECISIONS.md`、`FEATURE_MAP.md` 随候选提交锁定；最终 `release_sha` 记录在仓库外发版日志或可选 `telemetry_log`。

| 模式 | 固定执行顺序 | 关键门禁与失败处理 |
| --- | --- | --- |
| `own`（自研合主干打标） | 复核 `.loop/handoff.md` -> `pre-merge` -> 切目标分支（默认 `main`）优先 `git merge --ff-only` -> `git tag -a vX.Y -m "release vX.Y"` -> `post-tag --reviewed-sha <sha>` -> 仓外日志与清理端口 -> 按授权推送 -> `ship-record --mode own` | 仅当主干无分歧时才允许 `--no-ff`；主干已分歧则立即停手并退回 `/loop-build` 同步主干后重新审查；`post-tag` 或本轮要求的推送失败时从失败步骤续跑后再写 `.ship.json` |
| `external`（客串 PR 交接） | 删除 `.tmp/` 并核验端口空闲 -> 跑省略 `--tag` 的 `pre-merge` -> 输出面向 PR 的合并摘要 -> `ship-record --mode external` | 保持上游 `main`、标签与 `.loop/` 跟踪树零修改；跨工作区时按需传入 `--state-root` 与 `--docs-root` |

`reap_and_run.py --status --json` 按 `CLI --ports > .loop/config.json['ports'] > 18700-18799` 解析端口并支持 `none`，跨 worktree 传入 `--state-root <主工作区>` 或显式 `--ports <区间>`。

`ship-record` 支持可选 `--sampling-note "<抽检意见>"`（折叠空白后上限 500 字符，超限报 `[SAMPLING_NOTE_TOO_LONG]`，空白省略该键），写入 `.ship.json` 并在归档时同步写入 `.retro.json` 历史快照，`review-status` 输出省略该字段。

```bash
# 1. 自研模式：pre-merge -> ff-only 合主干 -> 打标签 -> post-tag -> 清理端口 -> ship-record
python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase pre-merge --tag vX.Y --json
git checkout main && git merge --ff-only <feature-branch>
git tag -a vX.Y -m "release vX.Y"
python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase post-tag --tag vX.Y --branch main --reviewed-sha <reviewed_sha> --json
python <loop-review-dir>/scripts/reap_and_run.py --status --cwd . --json
python <loop-review-dir>/scripts/devloop_ops.py ship-record --cwd . --mode own --tag vX.Y --reviewed-sha <reviewed_sha> --target-branch main --push-status local_only --sampling-note "<可选抽检备注>" --json

# 2. 客串模式：省略 --tag 执行 pre-merge 与 PR 交接凭据写入（跨 worktree 补 --state-root）
python <loop-review-dir>/scripts/reap_and_run.py --status --cwd . --state-root <主工作区> --json
python <loop-review-dir>/scripts/devloop_ops.py release-gate --cwd . --phase pre-merge --mode external --version vX.Y --branch <feature-branch> --reviewed-sha <reviewed_sha> --json
python <loop-review-dir>/scripts/devloop_ops.py ship-record --cwd . --mode external --version vX.Y --branch <feature-branch> --reviewed-sha <reviewed_sha> --sampling-note "<可选抽检备注>" --json
```

`telemetry_log` 标准 `released` JSONL 字段：`{"event":"released","tag":"vX.Y","reviewed_sha":"...","release_sha":"...","mode":"own"}`。

## 三、清理与交接下步

删除 `.tmp/` 与临时测试 worktree，确认 `reap_and_run.py --status --cwd . --json`（跨 worktree 加 `--state-root <主工作区>`）的 `listening` 为空且 `git status --porcelain` 为空。保留 `.loop/reviews/<scope-key>/` 与 `.loop/reviews/<scope-key>.ship.json` 活跃不归档，提示用户执行 `/loop-retro` 完成工作环境复盘与归档收尾。
