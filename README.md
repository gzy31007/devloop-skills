# devloop-skills

跨编码 Agent 通用研发闭环技能套件（支持 Antigravity、Claude Code、Codex、OpenCode 等）。

## 安装与更新

```bash
# 全局安装
npx skills add gzy31007/devloop-skills -g

# 或装到单个项目目录
npx skills add gzy31007/devloop-skills
```

## 包含技能

- `agent-docs`：面向 Coding Agent 的文档与注释写作规范（短段落、表格优先、真实代码、现在时态、零 emoji、源码事实核验）
- `hakimi`：总路由与项目现状侦察，只指路并给出下一步斜杠命令
- `loop-init`：项目首次接入（自动识别 Python / TS / JS 技术栈，支持自研与客串双模式）
- `loop-plan`：边界设计、原型比选与可推翻的 `DECISIONS.md`
- `loop-build`：分支编码、缺陷先红后绿、自测门禁与交接
- `loop-review`：Breaker 实锤独立审查（写审单次隔离、真实结果优先、Web 浏览器实测闭环、形式化/属性检验留痕与附带门禁脚本）
- `loop-retro`：发版复盘与分层固化
