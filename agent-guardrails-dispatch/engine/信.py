# -*- coding: utf-8 -*-
"""信.py —— 跨窗信箱的机械载体：发 / 箱 / 已读。

⭐⭐ 为什么要脚本（用户 2026-08-13 裁定「文件协议靠记忆，脚本是机械」）：
  信箱协议里有五件事全靠模型"记得"就必然漂移——命名格式、级别标注、落哪个目录、
  发完该不该按门铃、怎么算已读。本脚本把它们全部固化成机械步骤，模型只出**内容**。

用法（在项目根目录下跑）：
  python <垫片>/信.py 发 --给 <角色> --题 "一句话" --从 <本窗署名·必填> [--级 红|黄|绿]
                        (--正文 <文件路径> | --正文 -) [--相关 文件1 文件2 ...]
  python <垫片>/信.py 箱                 列出未读（收件目录下非 已读/ 的信）
  python <垫片>/信.py 已读 <文件名...>    移进 已读/

级别语义（⭐ 收件窗的注意力保护——哨按此分级提醒，⛔ 本窗任务质量永远第一）：
  红 ＝ 对方正在做的事会因此**出错/撞车/白费**（前提错了、我在改同一处、你的判据反了）
       ⇒ 对方哨在下一次工具调用就提醒「收尾手头原子步后立即读」；⭐ 发完**必按门铃**
  黄 ＝ 有结论/纠错/交接要给对方，但不影响它当前这一步
       ⇒ 对方在下一个自然边界被提醒；门铃可选
  绿 ＝ FYI / 回执 / 落账
       ⇒ ⛔ 完全不打扰工作中的窗，对方**开窗时**才看到

⭐ 门铃（send_message）为什么脚本按不了、只能打印指引：
  哨与本脚本记的会话号（hook stdin 的 session_id）和 CCD 的 `local_*` 会话号**是两套独立编号**
  ——2026-08-13 实测：`get_session("local_<hook号>")` 查无此会话 ⇒ 纯脚本无法把"哪个窗"翻译成
  send_message 的收件地址。⇒ 门铃这步由**发信窗的模型**执行：list_sessions 按
  「cwd＝本项目 ＋ 标题像收件角色 ＋ isRunning/最近活跃」挑目标。本脚本负责把这条指引
  在发信那一刻打印到它眼前（同 codex派单 的 spawnBrief 模式：脚本钉步骤，模型执行）。
  ⚠️ send_message 对无人值守会话（定时任务/远程派发）不可用 ⇒ 那类窗永远走拉取兜底。
"""
import argparse
import os
import re
import sys
import time
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
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

级说明 = {"红": "对方手头的活会因此出错/撞车", "黄": "有结论要交接，不打断对方当前步骤",
        "绿": "FYI/回执/落账，开窗才提醒"}
# ⭐ 纯回执/确认「不影响你」类 ⇒ 用绿，⛔ 不用发明比绿更低的档（2026-08-14 实测：
#   有窗想要「蓝/低」——绿的语义本来就盖住回执，是文档没写例子，已补进 跨窗协作_怎么用.md §二）。


def _项目根():
    """⭐ 优先认 `$CLAUDE_PROJECT_DIR`，⛔ 不靠 cwd。

    ⚠️ 2026-08-13 上线当天就投错一次：shell 停在基建仓目录，脚本按 cwd 往上找 `.claude/`，
    把信落进了**没有任何窗在看的那个仓**。⇒ cwd 是**隐式参数**，而隐式参数迟早会错——
    这和 hook 命令那次「相对路径按进程 cwd 解析导致全会话卡死」是同一个形状。
    ⇒ 有显式锚点就用显式锚点；cwd 只作最后退路，且**投递时把落点打印出来让人看得见**。
    """
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / 配置目录).is_dir():
        return Path(env).resolve()
    d = Path.cwd().resolve()
    for p in (d, *d.parents):
        if (p / 配置目录).is_dir():
            return p
    return d


def _收件目录(根, 建=False):
    rel = "_dev/收件"
    f = 根 / 配置目录 / "工作流.yaml"
    if f.is_file():
        try:
            import yaml
            rel = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("收件目录") or rel
        except Exception:
            pass
    d = 根 / rel
    if 建:
        (d / "已读").mkdir(parents=True, exist_ok=True)
    return d


def _解析目标项目(名或路径):
    """`--给项目` 的取值 ⇒ 项目根。⭐ 支持三种写法：绝对路径 / 相对本机的项目名 / 兄弟目录名。

    ⚠️⚠️ 2026-08-14 立此参数的原因（实测，⛔ 不是设想）：
      本函数上面那个 `_项目根()` 的「显式锚点」`$CLAUDE_PROJECT_DIR` **在模型跑 Bash 时是空的**
      （实测 `echo $CLAUDE_PROJECT_DIR` ⇒ 空）——它只在 hook 命令里被展开。
      ⇒ 送信落点**实际 100% 由 cwd 决定**，那道"防投错"的保险**一直是空转的**。
      ⇒ 跨项目送信时，窗很容易把信落进**自己的**收件箱，而对面永远看不到（⭐「静静地不到」）。
    ⇒ 所以给一个**显式**参数：写明发给哪个项目，⛔ 不靠"记得先 cd"。
    """
    p = Path(名或路径)
    # ⭐ 先查关系册别名（2026-08-14）：两台机文件夹名不同（如同一项目在两台机上叫不同的文件夹名）
    #   ⇒ 用户/模型只记得「项目名」，实际目录名交给册里的 `别名:` 兑换，⛔ 不要求两机同名。
    名单 = [名或路径]
    try:
        import yaml
        # ⚠️⚠️ 2026-08-16 由 codex 窗实测抓出：这里原来只按「引擎目录的上一级」找册子，
        #   ⛔ 没读适配层设的 `WORLDBOOK_关系册` ⇒ 插件形态下册子在**数据目录**里，这儿找不到
        #   ⇒ 现象很阴：开窗注入同事册**正常**、给绝对路径发信**正常**，
        #     唯独「用册子里的正名/别名发信」找不到目标 —— 三条路里坏了一条，最难察觉。
        # ⭐ 本窗当时只给 哨.py 加了这个环境变量、漏了本文件 ——
        #   正是 codex 上一轮警告过的「同一件事在两个文件里判两遍，迟早不一致」，我当场犯了。
        册文 = Path(os.environ.get("WORLDBOOK_关系册")
                    or (Path(__file__).resolve().parent.parent / "关系册.yaml"))
        if 册文.is_file():
            for 正名, 项 in ((yaml.safe_load(册文.read_text(encoding="utf-8")) or {}).get("项目") or {}).items():
                别名 = [str(x) for x in ((项 or {}).get("别名") or [正名])]
                if 名或路径.lower() in [x.lower() for x in 别名 + [正名]]:
                    名单 = 别名 + [正名]
                    break
    except Exception:
        pass
    # ⭐ 兄弟目录从**本机登记的库路径**推，⛔ 不写死盘符（用户 2026-08-15 裁定：一个盘符都不许出现）。
    #   ⚠️⚠️ 这里原来写死了某台机器的绝对路径 —— 另一台机上那个目录根本不存在，
    #     ⇒ 跨项目送信在那台机上只剩 cwd 和项目同级两条路，而这跟"对面没收到"长得一样。
    #     ⭐ 垫片当时已按三级查找改对（模板/垫片_哨.py），本文件漏了。
    兄弟根 = []
    try:
        册 = 宿主用户目录 / "worldbook路径"
        if 册.is_file():
            for 行 in 册.read_text(encoding="utf-8", errors="replace").splitlines():
                行 = 行.strip()
                if 行 and not 行.startswith("#"):
                    兄弟根.append(Path(行).expanduser().parent)
    except Exception:
        pass
    候 = [p] if p.is_absolute() else []
    for 名 in 名单:
        候 += [Path.cwd() / 名, _项目根().parent / 名] + [b / 名 for b in 兄弟根]
    for c in 候:
        try:
            if (c / 配置目录).is_dir():
                return c.resolve()
        except OSError:
            pass
    return None


def 发(a):
    if getattr(a, "给项目", None):
        根 = _解析目标项目(a.给项目)
        if 根 is None:
            # ⚠️ 2026-08-16 codex 实测点名：这句原本硬写 `.claude/`，
            #   而 codex 宿主下配置目录叫 `.codex/` ⇒ **功能是好的，只有报错文案会误导排查**。
            #   ⭐ 「错的提示比没有提示更贵」——它会把人引到不存在的问题上。
            print("⛔ 找不到项目 `%s`（要求它有 `%s/` 目录）。试过：绝对路径、当前目录下、"
                  "本项目同级、本机登记的库目录同级。⇒ 直接给绝对路径最稳。"
                  % (a.给项目, 配置目录))
            return 1
    else:
        根 = _项目根()
    d = _收件目录(根, 建=True)
    if a.正文 == "-":
        正文 = sys.stdin.buffer.read().decode("utf-8", "replace")
    else:
        p = Path(a.正文)
        if not p.is_file():
            print("⛔ 找不到正文文件：%s（用 --正文 - 走 stdin 也行）" % p)
            return 1
        正文 = p.read_text(encoding="utf-8", errors="replace")
    if not 正文.strip():
        print("⛔ 信不能没有正文——门铃只是铃，内容必须落在信里（信是唯一耐久载体）")
        return 1
    题safe = re.sub(r'[\\/:*?"<>|\s#]+', "", a.题)[:40] or "无题"
    名 = "收件_%s_给%s_%s_%s.md" % (a.级, a.给, 题safe, time.strftime("%Y%m%d-%H%M"))
    头 = (
        "---\n给: %s\n从: %s\n级: %s   # %s\n题: %s\n相关: [%s]\n时间: %s\n---\n\n"
        % (a.给, a.从, a.级, 级说明[a.级], a.题, ", ".join(a.相关 or []),
           time.strftime("%Y-%m-%d %H:%M"))
    )
    (d / 名).write_text(头 + 正文.rstrip() + "\n", encoding="utf-8")
    # ⭐ 「已投：」这一行是哨认作者的凭据（PostToolUse 扫工具输出里的这个标记 ⇒ 发信人自己不被提醒）
    #    ⛔ 别改这两个字——改了发信窗会被自己的信提醒一次
    print("已投：%s" % 名)
    print("  收件目录：%s" % d)
    # ⭐⭐⭐ 铃-4（2026-08-17 两次真事故）：**原来这里无条件劝人去按门铃 —— 那是在把人往会丢的路上推。**
    #
    # ⚠️ 当天实测比分（⛔ 别删，这段文案的全部依据）：
    #   · 按铃**唤醒**对方：**0/3** —— 一次对方进程 18 分钟后死掉、
    #     两次**轮次根本没建立**（消息到了、看得见，但没启动对方那一轮）；
    #     ⭐ 注意分开记：**到达 3/3 是好的，唤醒 0/3 才是坏的**。
    #   · 用户**手动**唤醒：3/3 成功；· **写信**：4/4 送达；· 扫转录：全部拿到且对方零消耗。
    # ⚠️ 而发起方那一侧**全是绿的**（返回已送达／账本+1／提示词照贴）⇒ ⛔ 自己分不出对方死没死。
    #
    # ⭐ 于是把「先看对方在不在跑」从**建议顺序里的一个字**提成**前置判据**：
    #   ⚠️⚠️ **⛔ 别写成「睡着的才叫不醒」** —— 那个归因被 10:20 那封红信自己撤回了
    #     （唯一一次「目标醒着」的实验没成立）⇒ **变量至今没翻转过，两种解释都活着**。
    #   ⇒ 所以这里给的是**不依赖那个未决问题**的判据：
    #     要对方**现在就动** ⇒ ⛔ 光按铃不够，请用户去喊（手动唤醒当天 3/3）；
    #     内容**一律落在信里**（写信 4/4 没丢过）。
    # ⛔ 本脚本查不到 isRunning（`get_session` 是 MCP 工具，脚本调不到，同「查模型」那条）
    #   ⇒ 这一步**只能由模型做** ⇒ 这里把它写成**必须先做的一步**，⛔ 不再是可选项。
    if a.级 in ("红", "黄"):
        print(
            "\n⭐ %s级 ⇒ 信已落盘。**按门铃之前先做这一步，⛔ 别跳**：\n"
            "   ① `list_sessions` 找目标（cwd＝%s ＋ 标题像「%s」），"
            "再 `get_session` 看它的 **isRunning**：\n"
            "      · **在跑** ⇒ 可以按铃，正文一句话即可：\n"
            "        『📬 %s级新信：%s ——读 %s』\n"
            "      · **没在跑** ⇒ ⛔ 别按。\n"
            "   ⚠️⚠️ 但先知道这条：当天实测 **3 次按铃、0 次真的唤醒对方** ——\n"
            "      消息**到得了**（对方窗口里看得见），但它**⛔ 不会启动对方的一轮**；\n"
            "      而你这边**看起来全是成功的**。⛔ 「睡着才这样还是一直这样」目前**判不了**。\n"
            "      ⇒ 信已经落盘了，对方**下次开窗时哨会告诉它**（这条路当天 4/4 没丢过）；\n"
            "      ⇒ 要它**现在就动** ⇒ ⛔ 光按铃不够，**请用户手动把那个窗喊起来**（当天 3/3 成功）。\n"
            "   ② 挑不出唯一目标 ⇒ ⛔ 别按，信已落盘、对方哨的拉取会兜底。\n"
            "   ⛔ 无论哪种，**别把信的内容写进门铃**——门铃会丢（不落盘），信不会。"
            % (a.级, 根, a.给, a.级, a.题, (d / 名)))
        if a.级 == "红":
            print("   ⚠️ 红级：**紧急⛔ 不改变上面的判据** —— 按铃"
                  "⛔ 不会让对方更快读到（它不启动对方那一轮）。⇒ 急就请用户去喊。")
    else:
        print("  绿级：⛔ 不按门铃、⛔ 不打扰工作中的窗——对方开窗时哨会告诉它。")
    return 0


def 箱(a):
    d = _收件目录(_项目根())
    信们 = [p for p in sorted(d.glob("*.md")) if not p.name.lower().startswith("readme")] if d.is_dir() else []
    if not 信们:
        print("（信箱空）")
        return 0
    now = time.time()
    for p in 信们:
        m = re.match(r"^收件_(红|黄|绿)[_.]", p.name)
        print("  [%s] %s（%d 分钟前）" % (m.group(1) if m else "黄", p.name, (now - p.stat().st_mtime) / 60))
    return 0


def 已读(a):
    d = _收件目录(_项目根())
    (d / "已读").mkdir(parents=True, exist_ok=True)
    码 = 0
    for 名 in a.文件名:
        p = d / Path(名).name
        if not p.is_file():
            print("⛔ 没有这封信：%s" % p)
            码 = 1
            continue
        p.replace(d / "已读" / p.name)
        print("已归档：已读/%s" % p.name)
    return 码


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("发")
    p1.add_argument("--给", required=True, help="收件角色（执行窗/C-18窗/工作流窗…），⛔ 不是会话号")
    p1.add_argument("--题", required=True, help="一句话主题——收件窗靠它判断相关性，写具体")
    p1.add_argument("--级", default="黄", choices=["红", "黄", "绿"])
    p1.add_argument("--正文", required=True, help="正文文件路径，或 - 表示走 stdin")
    p1.add_argument("--相关", nargs="*", help="涉及的文件/单号（对方审查的入口）")
    # ⭐ 2026-08-14 改必填：可选时两次实测都发出了「从: 未署名窗」——同项目多窗并发时，
    #   回信方按 cwd＋标题挑门铃目标永远挑不出唯一命中 ⇒ 「挑不出就别按」⇒ 追问链路断死。
    #   ⚠️ 原提案「不填时自动用项目名＋会话标题兜底」做不了：会话标题在 CCD 侧，
    #   CLI 子进程拿不到（hook 会话号与 local_* 是两套编号，见文件头）⇒ 退而求其次：必填＋报错指路。
    p1.add_argument("--从", required=True,
                    help="本窗署名（必填）：项目名＋角色，例：某项目 主窗。"
                         "⛔ 不署名对方按不了门铃回追你")
    p1.add_argument("--给项目", default=None,
                    help="⭐ 跨项目送信必填：目标项目名或绝对路径（如 某项目名）。"
                         "⛔ 不填＝落进当前目录所属项目——跨项目时那就是投错，对面永远看不到")
    p2 = sub.add_parser("箱")
    p3 = sub.add_parser("已读")
    p3.add_argument("文件名", nargs="+")
    a = ap.parse_args()
    return {"发": 发, "箱": 箱, "已读": 已读}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
