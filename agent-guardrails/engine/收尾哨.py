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
   ③ ⭐ **别的项目有信落盘了却一直没人读**（2026-08-16 加，见下）
   ④ 项目附加检查（配置 `收尾哨附加: [命令…]`，如某账本项目的 L0 自检——
      2026-08-04 Baton 漂移挂了四天才被偶然撞见；有这一步，下一个 session 开工就抓住）

⭐⭐ ③ 为什么也归收尾哨（2026-08-16 立，某图像产线项目 主窗交单、用户亲历的事故）：
   执行窗发信问主窗要裁定后**停下等**；主窗把裁定落盘 ＋ 按了门铃；
   然后**两个窗先后随会话结束消失** ⇒ 那封信**七小时零人读** ⇒ 7 小时空转。
   ⚠️ 病根：信箱靠 hook 事件被看见（盲区＝窗开着但空闲、或**没有窗**）、
     门铃要求目标会话**此刻存活** ⇒ **两条通道都要求收信窗活着**。
   ⭐ 而「上一窗留下一封没人读的信」**和「上一窗没提交」是同一类账**——
     都是"上一窗走了、留了个尾巴、下一窗不知情"。⇒ 归这里，⛔ 不另造一个开窗提醒。
   ⚠️ 它**⛔ 不能替代**进程外定时巡检：人不开窗它就不响。⭐ 但「人回来时一定会开一个窗」
     是本仓唯一稳的人到场时机 ⇒ 它把发现时间从"用户想起来查"提前到"用户下次开窗"。

行为：干净且已推 ⇒ 一声不吭（⛔ 不加噪音）；有账 ⇒ 打警告进上下文。
⛔ 只查不拦（exit 恒 0）——收不收尾是判断活，脚本只负责让新窗口知情。
⛔ 不联网（不 fetch）：ahead 判断基于上次 fetch 的本地引用，开工仪式第一步本来就是 git pull。
"""
import os
import subprocess
import sys
from pathlib import Path

# ⭐⭐ 宿主无关（2026-08-16）：同一份引擎要能跑在不同宿主上
#   —— Claude Code 的项目配置在 `.claude/`，codex 在 `.codex/`。
#   ⛔ 别写死其中一个；也⛔ 别在每个引擎里各写一份"依次试"的查找函数
#     （同一件事判两遍迟早不一致——本仓的原罪就是这个）。
#   ⇒ 决策**只有一处**：适配层开窗时设好这两个环境变量，引擎照读。
配置目录 = os.environ.get("WORLDBOOK_配置目录名") or ".claude"
宿主用户目录 = Path(os.environ.get("WORLDBOOK_宿主用户目录") or (Path.home() / ".claude"))


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

# ③ ⭐⭐ 跨项目待应答：**2026-08-20 用户拍板摘掉**（原来在这儿开窗时扫所有项目并报出来）。
#   用户原话（判据在这儿，⛔ 别凭"当初立它有理由"就装回去）：
#     「信在哪个项目就只提醒授权文件夹为该项目的窗口。rp 那两个项目的信一直在提醒 UI 系列的项目，
#       但它们一点关系都没有，不该提醒。很多时候我忙不过来会暂时搁置一些项目，但可能会有偶尔的
#       灵感/工作流同步送过去暂时先放着。这些就到我打开这些项目的时候让该项目的窗口自己去看就好了。」
#   ⇒ ⭐ 关键认识：**搁置是有意的**，投给搁置项目的信本来就该在那儿等
#     ⇒ 跨项目念它 ⛔ 不是安全网，是纯噪音（实测：4 封等了 74.6 小时的黄/绿信，
#       在 7 个项目的**每一次开窗**都被念一遍，而收件人跟这些项目毫无关系）。
#   ⚠️ 如实记代价（⛔ 不粉饰）：立它的那个事故会回来一半——一封信落盘、收件项目长期没人开
#     ⇒ 没有任何东西会主动告诉你。⭐ 但用户明确接受：那正是他要的「先放着」。
#   ⭐ 同项目内的协作**不受影响**：本项目的信由 `哨.py` 的收件提醒管（开窗＋每次工具调用后），
#     ⛔ 那一半没动。
#   ⭐ 巡检脚本 `引擎/待应答.py` **保留**，改为**手动/按需**跑：
#       python <垫片目录>/待应答.py            # 扫所有项目，看谁还没回
#     总管模式那段话里也仍然指着它（总管有理由主动查一次）。

# ④ 项目附加检查（⛔ 引擎不认识任何项目的账本——项目的归项目，只负责替它跑）
try:
    import yaml
    f = 项目根 / 配置目录 / "工作流.yaml"
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
