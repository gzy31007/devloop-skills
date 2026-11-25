---
name: loop-build
description: 按计划开分支编码：先查审查状态读取打回原件、落实四项源头防错、缺陷先红后绿、跑真实结果与脏输入属性测试及架构门禁，填齐 .loop/handoff.md 后提交锁定候选提交。开始编码或返工时使用。
---

# loop-build · 分支编码与双向交接

脚本路径优先从已加载的 `loop-review` 技能目录拼接 `scripts/<脚本名>`，CI 与夹具优先读取 `DEVLOOP_KIT`。

## 一、返工入口与开分支防错

动手前先查询当前分支的审查状态，若最新裁决为 `reject`，先读取 `.loop/reviews/<scope-key>/` 下最新 `rNN-reject.md` 原件，按复现命令先写最小失败回归用例再修复根因，并将返工验证证据写入 `.loop/handoff.md`。

```bash
python <loop-review-dir>/scripts/devloop_ops.py review-status --cwd . --json
```

新开版本从约定基线拉出 `feat/<版本>-<主题>` 分支开发，主干合并与打标签交由 `/loop-retro` 处理。编写业务代码前默认落实以下四项源头防错：

| 防错项 | 实现要求 |
| --- | --- |
| BOM / CRLF 与原子写盘 | 文本读取统一剥离 UTF-8 BOM（`utf-8-sig` / `replace(/^\uFEFF/, "")`）并兼容 CRLF/LF；写盘采用临时文件加原子重命名，捕获 I/O 与解压异常并返回同形错误结构 |
| 入参前置拦截与生成文件忽略 | CLI 与 HTTP 共用参数在绑定端口或写盘前统一校验并保持原数据零改动；运行期生成的默认数据文件同时写入 `.gitignore` 与 `.loop/config.json` 的 `generated_paths` |
| 幂等防连点与统一寻址键 | 前端写操作按钮请求期间加锁防连点；同一资源的增删改查统一采用唯一稳定 ID（或全下标一致），杜绝同资源混用 ID 与数组下标 |
| 路径动态定位 | 脚本与测试按 `__file__` 或 `import.meta.url` 推导项目根，消除依赖目录层数的固定 `..` 相对路径 |

## 二、测试与门禁

| 类别 | 要求 |
| --- | --- |
| 缺陷回归 | 先写最小失败用例确认红灯，修复根因后并入回归套件确认转绿 |
| 真实结果验证 | 优先验证 CLI 输出、HTTP 响应、真实落盘文件与浏览器交互，拒绝仅 mock 内部函数的凑数单测 |
| 脏输入属性测试 | 涉及解析器、状态机、编解码或规则计算时配属性不变量测试（`hypothesis` / `fast-check`），固定随机种子、打印样本数，覆盖空值、UTF-8 BOM、CRLF、非结构行与边界极值 |

```bash
python <loop-review-dir>/scripts/reap_and_run.py --cwd . --timeout 600 -- <验证命令>
python <loop-review-dir>/scripts/arch_gate.py --json .
```

## 三、文档同步与锁定提交

交审前同步对应模式的 `DECISIONS.md`、`FEATURE_MAP.md`（自研在根目录，客串在 `.loop/`）与清单版本号（`package.json` / `pyproject.toml`），并按 `references/handoff.template.md` 填齐 `.loop/handoff.md`：

1. 填写版本、分支、基线 SHA、验收目标、改动范围、自测与返工验证证据、行为等价证据与已知待验项；
2. 逐行填齐「源头防错自检表」四项（命令与真实输出摘要）；
3. 在「文档同步声明」为 `DECISIONS.md` 与 `FEATURE_MAP.md` 各自填报 `已更新`（附关联条目与摘要）或 `本轮无适用变更`（附具体理由）。

将代码、文档、清单版本与 `.loop/handoff.md` 一并提交到当前源分支并确认 `git status --porcelain` 为空；候选 SHA 直接取提交后的 `git rev-parse HEAD`。

提交完成即停手。主路径提示用户新开干净会话（推荐跨模型或跨工具）输入 `/loop-review`；支持只读子代理或跨 Agent 协作工具的宿主也可按 `/loop-review` 兜底分支执行。
