---
name: loop-init
description: 为当前仓库接入 devloop 循环：识别技术栈、写入 .loop/config.json，按自研模式或客串模式落地追踪与本地影子配置。首次接入、换仓库、技术栈变化时使用。
---

# loop-init · 接入循环

## 一、识别技术栈

看仓库根的文件与目录，给出结论：

| 证据 | 栈 |
| --- | --- |
| `pyproject.toml` / `setup.py` / `*.py` | python |
| `tsconfig.json` / `*.ts` / `*.tsx` | ts |
| `package.json` / `*.js` / `*.jsx` | js |
| 以上多类同时出现 | 混合，写全列表 |

`languages` 写 `"auto"` 让门禁自己识别，或写明确列表，例如 `["python", "ts"]`。

## 二、选接入模式

### 自研模式（Own Repo）

`.loop/config.json` 提交进仓库跟踪，作为项目约定的一部分。缺省时一并创建 `FEATURE_MAP.md` 与 `DECISIONS.md`（用 `references/` 的模板）。队友克隆后直接拿到同一份配置。

### 客串模式（External Repo）

上游仓库自带 Lint / CI，全部沿用。本地这样落地：

1. `.loop/` 写进 `.git/info/exclude`，上游 `.gitignore` 与工作树零改动；
2. 在 `~/.devloop/projects/<repo-id>/config.json` 维护本地影子配置，`<repo-id>` 取仓库目录名的小写连字符形式；
3. 门禁优先读仓库内 `.loop/config.json`，缺省时回退影子配置。

```bash
printf '.loop/\n' >> .git/info/exclude
mkdir -p ~/.devloop/projects/<repo-id>
cp .loop/config.json ~/.devloop/projects/<repo-id>/config.json
```

## 三、落盘配置

以 `references/config.example.json` 为底，按仓库规模调 `max_lines`、`max_complexity`、`ignore_dirs`。写完后跑一次门禁确认基线：

```bash
python .loop/../skills/loop-review/scripts/arch_gate.py --json .
```

门禁报红就先修到绿，再进入 `/loop-plan`。

## 四、交接

把栈、模式、配置文件位置、门禁基线结果写进 `.loop/handoff.md`，下一步 `/loop-plan`。
