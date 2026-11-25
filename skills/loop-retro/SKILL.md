---
name: loop-retro
description: 发版后复盘，按「架构与门禁脚本 -> 行为测试 -> 技能文档」顺序沉淀教训，更新 FEATURE_MAP.md 并把过时取舍标为 superseded。发版完成后使用。
---

# loop-retro · 复盘沉淀

## 一、按顺序沉淀

1. **架构与门禁脚本**：能机械拦住的错误先交给脚本，写进 `arch_gate.py` 或 `verify_scripts.py`；
2. **行为测试**：拦不住的交给回归用例，名字写进行为地图；
3. **技能文档**：前两层都覆盖不到的经验，才写进 `SKILL.md`，保持精简。

顺序固定：脚本优先，测试其次，文档最后。同一条教训只落一层。

## 二、更新行为地图

`FEATURE_MAP.md` 补齐本版新增行为与对应检查；删掉已下线的行，历史版本号保留。

## 三、处理过时取舍

逐条读 `DECISIONS.md`，对照当前代码与最新需求：

- 前提仍成立：保留；
- 前提变了：重写该条，旧条目标 `[superseded by 2026-11-23 新条目摘要]`；
- 决策已删除：标 `[superseded by 删除原因]`。

## 四、写复盘

`.loop/handoff.md` 追加一段复盘：本版打回次数、每次根因、新增的机械门禁、下一版的减法重构检查点。数字与命令照抄实测输出。

## 五、收尾

工作树清理干净，临时目录删除，端口回收确认：

```bash
python skills/loop-review/scripts/reap_and_run.py --status
```
