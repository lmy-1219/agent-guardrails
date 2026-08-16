# -*- coding: utf-8 -*-
"""收尾哨 —— 每次开窗自动查上一窗的账（SessionStart hook 调用，⛔ 不靠记忆）。
⭐ 2026-08-09 起为跨项目统一引擎（收编四个项目各自的副本，
   以最新的 citypulse 版为基）。项目差异外置：`<项目>/.claude/工作流.yaml` 的 `收尾哨附加` 列表。

⭐ 为什么放在【开窗】而不是【收尾】：
   「记得收尾」没法用脚本保证（窗口关掉就关掉了）；
   但「忘了收尾」只在下一个窗口不知情时才有害 ⇒ 开窗时查账就把环闭上了。
   （原始裁决：用户 2026-08-07「该 git 的有没有 git……应该每次自动进行，改脚本而不是靠上下文记忆」）

查什么：
   ① 工作树未提交改动（所有项目）
   ② 本地领先远端＝忘 push（⭐ 自动探测：有 upstream 才查——双机项目忘 push ＝ 没交接；
      纯本地仓自然跳过，⛔ 不需要配置开关）
   ③ 项目附加检查（配置 `收尾哨附加: [命令…]`，如某账本项目的 L0 自检——
      2026-08-04 Baton 漂移挂了四天才被偶然撞见；有这一步，下一个 session 开工就抓住）

行为：干净且已推 ⇒ 一声不吭（⛔ 不加噪音）；有账 ⇒ 打警告进上下文。
⛔ 只查不拦（exit 恒 0）——收不收尾是判断活，脚本只负责让新窗口知情。
⛔ 不联网（不 fetch）：ahead 判断基于上次 fetch 的本地引用，开工仪式第一步本来就是 git pull。
"""
import os
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# 项目根＝hook 的工作目录（Claude Code 在项目根跑 hook）；也可 argv[1] 显式给
项目根 = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(os.getcwd()).resolve()
os.chdir(项目根)


def _git(*args):
    try:
        r = subprocess.run(["git", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=15)
        return (r.stdout or "").strip()
    except Exception:
        return ""


问题 = []

dirty = [l for l in _git("status", "--porcelain").splitlines() if l.strip()]
if dirty:
    问题.append("工作树有 %d 处未提交改动：" % len(dirty))
    问题 += ["   " + l for l in dirty[:10]]
    if len(dirty) > 10:
        问题.append("   …还有 %d 处" % (len(dirty) - 10))

sb = _git("status", "-sb")
if "ahead" in sb:
    问题.append("本地领先远端（忘了 push？）：" + sb.splitlines()[0])
    问题.append("   ⚠️ 双机项目忘 push = 没交接——另一台机器 pull 不到就看不见你做了什么")

if 问题:
    head = _git("log", "-1", "--format=%h %s (%cr)")
    print("⚠️⚠️ 收尾哨：上一窗可能没收尾——")
    for l in 问题:
        print(l)
    print("最后一次提交：" + head)
    print("⇒ 先判断这些改动属于谁（上一窗的活？半成品？执行方在制品？），补收尾再开工。")
    print("⇒ ⛔ 别不明就里 git add -A 提交——先看内容再定。")

# ③ 项目附加检查（⛔ 引擎不认识任何项目的账本——项目的归项目，只负责替它跑）
try:
    import yaml
    f = 项目根 / ".claude" / "工作流.yaml"
    if f.is_file():
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for cmd in d.get("收尾哨附加") or []:
            try:
                subprocess.run(cmd, shell=True, timeout=60)
            except Exception:
                pass
except Exception:
    pass
sys.exit(0)
