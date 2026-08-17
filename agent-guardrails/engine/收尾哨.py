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

# ③ 跨项目待应答：别的项目有信落盘了却没人读 ⇒ 开窗时报给眼前这个人
#   ⭐ 三条防噪：只报**超时**的 · **排掉本项目**（本项目的信由哨的收件提醒管，⛔ 别报两遍）
#     · 整段包在 try 里（⛔ 巡检出问题绝不许拖累开窗）。
#   ⛔ 没有超时的 ⇒ **一声不吭**（同本文件"干净就不出声"的既定纪律）。
try:
    import importlib.util as _iu
    _待 = Path(__file__).resolve().parent / "待应答.py"
    if _待.is_file():
        _s = _iu.spec_from_file_location("_待应答", _待)
        _m = _iu.module_from_spec(_s)
        _s.loader.exec_module(_m)
        # ⭐ 父目录＝库根的上一级（项目们平铺在那儿）。⚠️ 与 `接入.py --扫` 同一口径。
        _超时, _全部, _ = _m.扫全部(Path(__file__).resolve().parent.parent.parent,
                                    排除项目=项目根)
        if _超时:
            print("")
            print("🔔🔔 收尾哨 · **别的项目有 %d 封信落盘了却一直没人读**"
                  "——对面很可能正停下来等它。" % len(_超时))
            for _x in _超时[:5]:
                print("   · [%s] **%s** 等了 **%.1f 小时**（%s → %s）：%s"
                      % (_x["级"], _x["项目"], _x["等了小时"], _x["从"], _x["给"], str(_x["题"])[:60]))
            if len(_超时) > 5:
                print("   · …另有 %d 封" % (len(_超时) - 5))
            print("⇒ ⭐ **只有人能解开这个**：去那个项目开一个窗读它。"
                  "⛔ 机制叫不醒一个已经关掉的窗——")
            print("   所以这条是给**用户本人**的，⭐ 请**转述给用户**，⛔ 别自己消化掉。")
            print("   （立此条的事故：一封裁定信落盘＋按了门铃，两个窗随即消失 ⇒ 七小时零人读。）")
except Exception:
    pass      # ⛔ fail-open：巡检坏了不许弄坏开窗。⚠️ 代价＝它也会静默——见 STATE 挂账

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
