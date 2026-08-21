# -*- coding: utf-8 -*-
"""哨 —— 纠偏库引擎 ＋ 压缩闸（Claude Code hooks 通用入口）。

五个入口（argv[1]，缺省时读 stdin 的 hook_event_name）：
  UserPromptSubmit  ⭐ 唯一读得到**用户说了什么**的挂点。管两件：用户说「解限」⇒ 当场落总管
                授权并把纲领交到眼前；`事件: UserPromptSubmit` 的条目（现只有 JF-102）。
                ⛔ 绝不 block——拦住用户的输入是最糟的失败模式。
  PreToolUse    工具**跑之前**拦一次要求自查（自查后原样重发即放行）。⭐ 唯一能在
                「我正要动手」那一刻进上下文的载体——治「纪律只写在文档里、不会自动到执行者眼前」
  PostToolUse   扫最近一次工具输出/工具输入/连败序列/会话累计，命中 ⇒ additionalContext 注入
  Stop          扫本回合最终回复，命中条目 ⇒ block 一次要求自查（自查后放行，⛔ 不硬拦）
  SessionStart  压缩闸（量热层行数，超限报警）＋ staging 候选提醒

设计纪律（早期评估台账 E-12/E-13 定的，⛔ 别改松）：
  · fail-open：任何异常 ⇒ 无输出、exit 0。哨永远不许弄坏用户的会话。
  · 循环防护自己做（⛔ 平台不保证）：Stop 条目每会话最多 block 1 次；
    全会话 block 硬上限 2 次；stdin 带 stop_hook_active 真值 ⇒ 直接放行。
  · 冷却：PostToolUse 条目每会话 ≤ 冷却 次（默认 2）——噪音比不弹还糟。
  · 条目＝数据（active/*.yaml），脚本＝通用。⛔ 不在代码里写死任何一条方法论。
  · 实测采样内置：每次被调都把 stdin 顶层字段记进 _state/实测采样.jsonl
    （E-13 前置 1「实测 PostToolUse 的 stdin 到底给什么字段」——⛔ 不猜文档，攒真实样本）。
    ⇒ 引擎因此⛔ 不假设 stdin 有 tool_response：拿不到就退回解析 transcript_path。
"""
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE_DIR = Path(os.environ.get("WORLDBOOK_ACTIVE_DIR") or (ROOT / "active"))
STAGING_DIR = Path(os.environ.get("WORLDBOOK_STAGING_DIR") or (ROOT / "staging"))
STATE_DIR = Path(os.environ.get("WORLDBOOK_STATE_DIR") or (ROOT / "_state"))

# ⭐⭐ 宿主无关（2026-08-16）：同一份引擎要能跑在不同宿主上
#   —— Claude Code 的项目配置在 `.claude/`，codex 在 `.codex/`。
#   ⛔ 别写死其中一个；也⛔ 别在每个引擎里各写一份"依次试"的查找函数
#     （同一件事判两遍迟早不一致——本仓的原罪就是这个）。
#   ⇒ 决策**只有一处**：适配层开窗时设好这两个环境变量，引擎照读。
配置目录 = os.environ.get("WORLDBOOK_配置目录名") or ".claude"
宿主用户目录 = Path(os.environ.get("WORLDBOOK_宿主用户目录") or (Path.home() / ".claude"))


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
CODE_EXT_RE = re.compile(
    r"\.(py|js|mjs|cjs|ts|tsx|jsx|java|go|rs|c|cc|cpp|h|hpp|cs|rb|php|sh|ps1|sql|lua|kt|swift)$", re.I)
# 「真跑过一次」的证据：执行器/测试/请求类命令。⛔ git/ls/cd/echo 这类不算验证
RUN_CMD_RE = re.compile(
    r"(\bpython\b|\bpy\b|\bnode\b|\bnpm\b|\bnpx\b|\bpnpm\b|\byarn\b|\bpytest\b|\bgo (run|test)\b"
    r"|\bcargo\b|\bdotnet\b|\bjava\b|\bmvn\b|\bgradle\b|\bmake\b|\bbash \b|\bsh \b|\bpwsh\b"
    r"|\bcurl\b|\bjest\b|\bvitest\b|\bmocha\b|\bphpunit\b|\brspec\b|test|\.py\b|\.ps1\b|\.sh\b)", re.I)
WRITE_CMD_RE = re.compile(
    r"(>>?|Set-Content|Out-File|Add-Content|Tee-Object|New-Item|Move-Item|Copy-Item"
    r"|git\s+(add|commit|mv)|\bmv\b|\bcp\b|\btee\b|\btouch\b)", re.I)
# 「这一会话已经走过派单通道」的证据（累计闸的条件卫兵用；派过就别再催）
DISPATCH_CMD_RE = re.compile(r"(codex派单|codex\s+exec|派单\.py)", re.I)
# 连败判定用强标记（⛔ 不用「失败/错误」这种会出现在正常内容里的弱词——误报率是生死线）
STRONG_FAIL_RE = re.compile(
    r"(Traceback \(most recent call last\)|is not recognized as|command not found"
    r"|Permission denied|拒绝访问|No module named|SyntaxError:|FileNotFoundError"
    r"|exit code [1-9]|ExitCode[:：]\s*[1-9])", re.I)

# ⭐ 自指防护（2026-08-08 上线首日两次实测误报反推出来的，⛔ 别删）：
#   误报 A：回复里【转述】条目触发词（如「要以『做不了』收工时拦一次」）被当成认输 ⇒ 匹配前剥引用/代码；
#   误报 B：读纠偏库自己的日志/条目（触发日志的命中片段、注入文本）再次命中正则 ⇒ 含库指纹的文本整体跳过。
#   取舍已想过：谈论纠偏库的回合会成为盲区——认了。噪音毁信用比漏提醒更糟（方法论 九：
#   会喊狼来了的自查比没有更糟）。
# ⛔⛔ 2026-08-14 实测事故：把**库自己的路径**列进自指指纹，让护栏**对库自己的窗近乎全盲**——
#   该仓当天刚升为独立项目，它的窗几乎每条命令都带库路径（`cd <库路径> && …`）
#   ⇒ **每一次工具调用都被整条跳过**。实证：本窗一天内 4 次犯 JF-014（内联脚本过 shell 撞编码），
#     `触发日志` 里 JF-014 **0 条**——⛔ 不是"没犯"，是**看不见**（⭐ 又一个「没有」≠「没查到」）。
#   ⇒ ⭐ 改法：**只认库自己的内容指纹，⛔ 不认路径**。路径会出现在一切正当命令里，
#     而 `触发正则:`/`注入文本:`/`【纠偏 · 』/`命中片段` 这些才真是"库在讲自己"的字样。
#   ⚠️ 代价想清楚了：读库里 yaml/日志**原文**的回合仍会被跳过（那正是误报 B 要防的），
#     但**仅仅路过这个目录⛔ 不再致盲**。
SELF_RE = re.compile(r"【纠偏 · |触发正则|注入文本|命中片段|来源事故|JF-\d{3}")
# ⚠️ 2026-08-17 补上圆括号：那次误判的原句是「…权限可见性（**被解限**）的需求」——
#   触发词就藏在**括号里**。括号里的是旁注/词条，与引号同性质，⛔ 不是在下命令。
QUOTE_SPAN_RE = re.compile(r"「[^」\n]{0,200}」|『[^』\n]{0,200}』|“[^”\n]{0,200}”|`[^`\n]{1,200}`"
                           r"|（[^）\n]{0,200}）|\([^)\n]{0,200}\)")
FENCE_RE = re.compile(r"```.*?```", re.S)


def _is_self_content(text):
    return bool(SELF_RE.search(text or ""))


def _strip_quoted(text):
    """匹配前净化：剥掉围栏代码与引号内的转述，只留作者自己的陈述。"""
    return QUOTE_SPAN_RE.sub("", FENCE_RE.sub("", text or ""))


# ---------- 基础设施 ----------

def _可写(s):
    """把**孤立代理项**（`\\udcXX`）换掉再写盘。⛔ 少这一步，写 utf-8 文件会当场抛 UnicodeEncodeError。

    ⚠️⚠️ 2026-08-13 实证（错误日志 563 条里 **552 条**是这一个）：Windows 上工具输出／文件路径
    含非 UTF-8 字节时，Python 用 surrogateescape 把它转成 `\\udcXX`；`json.dumps(ensure_ascii=False)`
    原样留着它，**写文件那一刻才炸**。⇒ 三处写盘（采样／触发日志／会话状态）**全被同一颗雷打穿**，
    又因为处处 fail-open 而全被吞掉：
      · 实测采样丢了几百条 ⇒ 「某条目从没触发过」这种结论是**拿残账算出来的**；
      · 触发日志少记 15 次（JF-007 最惨——它的命中片段就是**文件路径**，最容易带非法字节）；
      · 会话状态写不进去 ⇒ 冷却计数、`派过单` 标志**一起丢**。
    ⭐ 病名：**fail-open ＋ 没人看错误日志 ＝ 静默烂掉**。fail-open 是对的，⛔ 但它必须配一个
      「错误攒够了就喊人」的哨——否则等于把故障扫进地毯下面（本次同批补上，见 do_session_start）。
    """
    try:
        return s.encode("utf-8", "replace").decode("utf-8", "replace")
    except Exception:
        return ""


def _log_error():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = STATE_DIR / "错误.log"
        if p.exists() and p.stat().st_size > 200_000:
            # ⭐ 留后半截，⛔ 不整份清空——整份清空会把"到底出了多少次"这个量也一起抹掉
            旧 = p.read_text(encoding="utf-8", errors="replace")
            p.write_text(旧[len(旧) // 2:], encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(_可写("---- %s ----\n%s\n"
                          % (time.strftime("%Y-%m-%d %H:%M:%S"), traceback.format_exc())))
    except Exception:
        pass


def _read_stdin():
    """⭐⭐ 按**二进制**读再显式 utf-8 解码，⛔ 不用 `sys.stdin.read()`。

    ⚠️⚠️ 2026-08-13 实测事故（比"写盘炸掉"那条更根本，且藏得更深）：
    hook 是被 `python "…/哨.py" <事件>` 直接拉起的，环境里**没有 PYTHONIOENCODING**
    ⇒ Windows 上 `sys.stdin` 按 locale（cp1252/mbcs）解，UTF-8 的中文当场变**双重编码乱码**：
        `自查.py` → `è‡ªæÿ¥.py`
    ⇒ **凡是带中文的「触发正则」都匹配不上**（JF-007 盯的就是 `设计规范`/`蓝湖` 这类路径），
      条目看着在库里、其实对中文输入是瞎的。
    ⚠️ 它长期没被发现，是因为 `_tail_records()` 读 transcript 时**显式指定了 utf-8** ⇒ 退路是对的
      ⇒ 部分条目照样能触发，**整体只是"半瞎"而不是"全死"**——最难查的那种。
    ⚠️ 而回归测试一直是绿的，因为测试进程继承了开发者 shell 里的 `PYTHONIOENCODING=utf-8`
      ⇒ **测试环境与生产环境在正好要紧的那一维上不同**。⭐ 测试侧已同批剥掉该变量（见 测试_哨.py）。
    """
    try:
        buf = getattr(sys.stdin, "buffer", None)
        raw = buf.read().decode("utf-8", "replace") if buf is not None else sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _采样归档目录():
    return STATE_DIR / "采样归档"


def _dump_sample(event, data):
    """实测采样：记 stdin 顶层字段名＋截断原文。E-13 前置 1 的证据就从这里攒。

    ⭐⭐ 铃-8（2026-08-17 真事故）：**截断前必须先归档。**
      ⚠️ 原来超 1MB 就直接砍成最后 100 行 —— ⛔ 不归档、⛔ 不出声、⛔ 不留痕。
        当天真丢了 **191 条**：另一个窗 10:0x 记的基线 304 条、10:2x 只剩 113 条，
        而它**分不出**是有人手动清的还是引擎自己截的 ⇒ 沉默有两种含义。
      ⚠️ 丢掉的正是「各挂点实际拿到什么字段」的原始数据 ——
        判「某条规则到底触没触发过」全靠它；今天定死铃-5 的病因也是靠它。
      ⭐ 1MB 上限本身是对的（⛔ 别让它无限长），错的是**砍掉那部分没了**。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = STATE_DIR / "实测采样.jsonl"
        if p.exists() and p.stat().st_size > 1_000_000:
            全 = p.read_text(encoding="utf-8", errors="replace").splitlines()
            留, 丢 = 全[-100:], 全[:-100]
            if 丢:
                # ⭐ 归档写在**截断之前**：万一这一步炸了，原文件还完整
                #   ⇒ 最坏是"这次没截成"（文件继续长），⛔ 不是"数据没了"。
                d = _采样归档目录()
                d.mkdir(parents=True, exist_ok=True)
                名 = "采样_%s.jsonl" % time.strftime("%Y%m%d-%H%M%S")
                (d / 名).write_text("\n".join(丢) + "\n", encoding="utf-8")
                _log_trigger("采样归档", event, "归档 %d 条 ⇒ %s（主文件留 %d 条）"
                             % (len(丢), 名, len(留)), data.get("session_id"))
            p.write_text("\n".join(留) + "\n", encoding="utf-8")
        rec = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "event": event,
            "keys": sorted(data.keys()),
            "raw": json.dumps(data, ensure_ascii=False)[:6000],
        }
        with p.open("a", encoding="utf-8") as f:
            f.write(_可写(json.dumps(rec, ensure_ascii=False)) + "\n")
    except Exception:
        _log_error()


def _log_trigger(entry_id, event, snippet, session_id=None):
    """触发日志：量误报率的原始数据（E-13 前置 5）。

    ⭐⭐ 这份是 **append-only**，因此它——⛔ 而不是会话状态里的 counts——才是「某条目触发过几次」
    的唯一可信账本。会话状态的 counts 只是**冷却簿记**：会话文件会过期、会被覆盖、损坏时还会重置，
    ⛔ 拿它做统计必然偏。（2026-08-13 实测：两本账 38 vs 17，两边**各自**都在丢。）
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # W-2 复核修复（回单 §五 a）：触发记录带会话短号，才分得清命中来自哪个窗口。
        会话 = str(session_id)[:8] if session_id else "?"
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "条目": entry_id, "会话": 会话,
               "event": event, "命中片段": (snippet or "")[:200]}
        with (STATE_DIR / "触发日志.jsonl").open("a", encoding="utf-8") as f:
            f.write(_可写(json.dumps(rec, ensure_ascii=False)) + "\n")
    except Exception:
        _log_error()


def _记评估(本轮, entry_id, 字段):
    """只攒本次 hook 的增量；真正写盘由入口结束时合并一次。"""
    d = 本轮.setdefault(str(entry_id), {"评估": 0, "条件挡": 0, "命中": 0})
    d[字段] += 1


def _合并评估计数(本轮):
    """仪表账 fail-open：旧账坏了留 `.坏`，本轮仍从空账重新累计。"""
    if not 本轮:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = STATE_DIR / "评估计数.json"
        账 = {}
        if p.is_file():
            try:
                账 = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(账, dict):
                    raise ValueError("评估计数不是对象")
            except Exception:
                # W-2 复核修复（回单 §五 a）：坏仪表从空重计，但原文件改名留证，⛔ 不静默覆盖。
                坏 = p.with_name(p.name + ".坏")
                if 坏.exists():
                    坏 = p.with_name(p.name + ".坏." + str(int(time.time())))
                p.replace(坏)
                账 = {}
        for eid, 增 in 本轮.items():
            d = 账.setdefault(eid, {"评估": 0, "条件挡": 0, "命中": 0})
            for 字段 in ("评估", "条件挡", "命中"):
                d[字段] = int(d.get(字段, 0)) + int(增.get(字段, 0))
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(_可写(json.dumps(账, ensure_ascii=False, indent=1)), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception:
        pass              # ⭐ 仪表坏了也⛔ 不许拖垮 hook


def _心跳(session_id):
    """每次开窗往触发日志写一行心跳，⛔ 哪怕一条都没触发；并**当场自检记录通路**。

    ⭐⭐ 由来（2026-08-13，另一个窗从**它自己的会话内容**里作证，这是单看日志永远拿不到的证据）：
      「触发日志五天只有 21 行、某会话 0 行」被读成「条目没触发」——**错的**。
      那个会话**实际收到过多次注入**，只是 `PostToolUse` 那条写盘路径在丢、`Stop` 那条是好的。
      ⇒ 「日志空」有两种完全不同的原因，而在日志里**长得一模一样**：
           真没触发   vs   记录通路坏了
      ⇒ 加心跳后两者⛔ 再也混不了：
           **有心跳、无触发 ＝ 真没触发；连心跳都没有 ＝ 记录通路跪了。**
      ⚠️ 差一点就因为"五天没动静"去重写条目的触发正则——那会修错东西（条目其实是好的，
        JF-009 还真的拦停过一次并改变了行为）。

    ⭐ 心跳同时是**通路自检**：探针带中文与孤立代理项（正是打穿过本库的那两种字符），
      写完立刻读回来比对；对不上就在开窗时喊人，⛔ 不再等五天。
    返回：出问题时返回一句人话告警，正常返回 None。
    """
    探针 = "心跳自检·中文" + "\udc90"
    try:
        _log_trigger("心跳", "SessionStart", "会话 %s 开窗｜通路探针 %s" % (session_id, 探针), session_id)
        p = STATE_DIR / "触发日志.jsonl"
        末 = [l for l in p.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()][-1]
        rec = json.loads(末)
        if rec.get("条目") != "心跳" or _可写(探针) not in str(rec.get("命中片段") or ""):
            return ("🚑 纠偏库：**记录通路自检没过**——心跳写进去又读不回来（或内容对不上）。"
                    "⇒ 现在起「日志里没有」⛔ 不能当成「没触发」，先修通路。")
    except Exception:
        _log_error()
        return ("🚑 纠偏库：**记录通路自检失败**（写不进触发日志）。"
                "⇒ 「日志空」此刻⛔ 不构成「条目没触发」的证据，先看 `_state/错误.log`。")
    return None


def _state_path(session_id):
    sid = re.sub(r"[^0-9A-Za-z_-]", "_", str(session_id or "无session"))[:80]
    return STATE_DIR / ("会话_%s.json" % sid)


def _空状态():
    return {"counts": {}, "blocks_total": 0, "代码文件": [], "派过单": False,
            "写过": {}, "冲突已提示": {}, "上次重放字节": 0}


def _state_load(session_id):
    """⭐ 分清两件事：**文件不在**（新会话，正常）vs **文件在却读不出来**（损坏，⛔ 不许当没事）。

    ⚠️ 旧版把两者一并 `except` 掉、都返回空状态 ⇒ 损坏被当成「这个会话什么都没发生过」，
    冷却计数与 `派过单` 标志**静默归零**，而且没有任何痕迹。
    ⭐ 这正是「**缺失被当成否定**」那一类：拿不到信息时落进了一个具体结论（"没发生过"），
    ⛔ 而不是落进"判不了"。⇒ 现在损坏文件会被改名成 `.坏` 留证，并在开窗时喊人。
    """
    p = _state_path(session_id)
    try:
        if not p.exists():
            return _空状态()                    # 新会话，⛔ 不是异常
        st = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(st, dict):
            raise ValueError("状态文件不是对象")
        st.setdefault("counts", {})
        st.setdefault("blocks_total", 0)
        st.setdefault("代码文件", [])   # 本会话主窗自己写过的代码文件（去重，累计闸用）
        st.setdefault("派过单", False)  # 本会话是否走过派单通道（条件卫兵用）
        st.setdefault("写过", {})       # 本会话写过的**全部**文件 {路径: 时刻}（撞车卫兵用，⛔ 不限代码）
        st.setdefault("冲突已提示", {})  # 撞车卫兵已拦过的 {路径: 当时的外部mtime}——同一次外部改动只拦一次
        return st
    except Exception:
        _log_error()
        try:                                    # 留证：⛔ 不许被下一次保存无声覆盖掉
            p.replace(p.with_name(p.name + ".坏"))
        except Exception:
            pass
        return _空状态()


def _state_save(session_id, st):
    """⭐ 原子写：先写临时文件再 `os.replace`。

    ⚠️ 旧版直接 `write_text` ⇒ 写到一半抛异常（例如撞上孤立代理项）会留下**半截 JSON**，
    下次加载就是 JSONDecodeError ⇒ 状态被判损坏重置。⇒ 一个编码错误能连锁成「整个会话的
    冷却与标志全丢」。原子替换把「写坏」和「换上」拆开，⛔ 让半截文件永远不会被看到。
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        p = _state_path(session_id)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(_可写(json.dumps(st, ensure_ascii=False)), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception:
        _log_error()


def _load_entries(event):
    try:
        import yaml
    except Exception:
        return []
    out = []
    for p in sorted(ACTIVE_DIR.glob("*.yaml")):
        try:
            e = yaml.safe_load(p.read_text(encoding="utf-8"))
            if isinstance(e, dict) and e.get("事件") == event and e.get("id") and e.get("注入文本"):
                out.append(e)
        except Exception:
            _log_error()
    return out


def _scope_ok(entry, cwd):
    scope = entry.get("作用域", "全局")
    if scope in ("全局", None, ""):
        return True
    names = scope if isinstance(scope, list) else [scope]
    base = Path(cwd or os.getcwd()).name
    return base in names


# ---------- 条件卫兵（可写单个字符串或列表；⭐ 全部满足才算过） ----------

def _has_dispatch_cfg(cwd):
    """本项目配没配派单通道 ＝ 有没有 .claude/工作流.yaml（预设表就住那儿）。
    ⭐ 有了它，「该派单」类条目才是全局条目：没接派单基建的项目自动噤声，⛔ 不用靠作用域白名单维护。"""
    try:
        return (Path(cwd or os.getcwd()) / 配置目录 / "工作流.yaml").is_file()
    except Exception:
        return False


def _收件目录(cwd):
    """本项目的跨窗信箱。可在 .claude/工作流.yaml 里用 `收件目录:` 改，缺省 `_dev/收件`。

    ⭐⭐ 为什么要有它（2026-08-13 立）：多窗并行时，**真正有价值的发现来自"窗 A 审窗 B"**
      ——实证：跑切图那个窗给排 C-18 的窗纠出了「两条测试互相矛盾（被『2 红』这个数字掩盖）」
      和「判不了与不合共用出口 ⇒ 指纹距离 0.087/0.091 的候选被当成新资源切出来」，
      **没有一条是自审查得出来的**。
    ⚠️ 但纯「放个文件」⛔ 不构成通知：实证一次投递石沉大海，因为①引擎只 glob `*.yaml`
      ②那段只在开窗时跑。⇒ 信箱必须配一个**会自己看一眼**的载体，就是本函数的调用点。
    ⛔ 别把它放进 `staging/`——那是纠偏候选区，混进收件是类别错误。
    """
    try:
        根 = Path(cwd or os.getcwd())
        f = 根 / 配置目录 / "工作流.yaml"
        rel = "_dev/收件"
        if f.is_file():
            try:
                import yaml
                rel = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("收件目录") or rel
            except Exception:
                pass
        d = 根 / rel
        return d if d.is_dir() else None
    except Exception:
        return None


LETTER_LV_RE = re.compile(r"^收件_(红|黄|绿)[_.]")


def _信级(name):
    """信的级别取自文件名（`收件_红_…`）。⛔ 没标的按黄——旧信不因此失联。
    级别语义（⭐ 收件窗的注意力保护，用户 2026-08-13 定的第一原则「本窗任务质量仍为第一」）：
      红＝对方手头的活会因此出错/撞车/白费 ⇒ 收尾当前原子步后立即读；
      黄＝有结论/交接，但不影响它当前这一步 ⇒ 下一个自然边界读；
      绿＝FYI/回执 ⇒ ⛔ 完全不打扰工作中的窗，只在开窗时提。"""
    m = LETTER_LV_RE.match(name)
    return m.group(1) if m else "黄"


def _查收件(cwd, st, 含绿=False):
    """列出**本会话还没被告知过**的收件。⛔ 只读，⛔ 不改文件——归档归读信的人。

    ⭐ 防噪四条（⛔ 少一条它就会变成第二个"喊狼来了"）：
      ① 每封信对**每个会话**只提醒一次（`收件已看` 记在会话状态里）；
      ② **自己写的信不提醒自己**——写入时就记进 `收件已看`（见 `_accumulate`），
         这同时把「谁写的谁不审」落在了通知层，⛔ 不靠人自觉；
      ③ 一次最多列 5 封，且**只有真列出的才记已看**
        （⚠️ 初版把没列出的也记了 ⇒ 第 6 封会永远无人知晓——修）；
      ④ **绿级在工具后场合既不列也不记**（`含绿=False`），留给开窗——工作中的窗⛔ 不为 FYI 分心。
    """
    d = _收件目录(cwd)
    if d is None:
        return []
    看过 = set(x.lower() for x in (st.get("收件已看") or []))
    新 = []
    try:
        for p in sorted(d.glob("*.md")):
            key = p.name.lower()
            # ⚠️ 2026-08-18：收件箱自己的 README 也是 .md ⇒ 它被当成一封「永远没人读的信」
            #   ⇒ 每次提醒都**多算一封**，还会占掉「一次最多列 5 封」里的一格。
            #   ⭐ `引擎/待应答.py` 与 `引擎/信.py` 早就排掉了它 —— ⛔ 只有这儿没排
            #     （同一件事三处各写一遍的典型代价）。口径按那两处对齐。
            if key.startswith("readme"):
                continue
            if key in 看过 or key.startswith("readme"):
                continue
            级 = _信级(p.name)
            if 级 == "绿" and not 含绿:
                continue
            新.append((p.name, max(0, int((time.time() - p.stat().st_mtime) / 60)), 级))
    except Exception:
        _log_error()
    新 = 新[:5]
    for 名, _, _ in 新:
        st.setdefault("收件已看", []).append(名.lower())
    return 新


def _收件提醒(新, cwd):
    d = _收件目录(cwd)
    行 = "\n".join("  · [%s] %s（%d 分钟前写的）" % (级, n, m) for n, m, 级 in 新)
    级们 = {级 for _, _, 级 in 新}
    尾 = []
    if "红" in 级们:
        尾.append("⛔ 有**红**级 ⇒ 把手头这一个原子操作收尾（别丢下半截编辑），**然后立即读**"
                  "——红＝你正在做的事可能因此出错/撞车/白费。")
    if "黄" in 级们:
        尾.append("黄级 ⇒ 推进到**下一个自然边界**（本步验收/段落收尾）时读，⛔ 不打断当前推理链。")
    if "绿" in 级们:
        尾.append("绿级＝FYI，顺带读即可。")
    # ⭐⭐ 铃-5 后半（2026-08-17 实测）：**收件箱是整个项目共用的一个目录**，
    #   靠文件名里的抬头区分收件人 ⇒ 一封写给**别的窗**的信，同项目每个窗都会被提醒。
    #   ⚠️ 而原来这里写死一句「⭐ 这是**另一个窗写给你的**」——**那是断言，不是事实**：
    #     实测当天两次都不成立（① 抬头是别的窗 ② 干脆是本窗自己刚投的）。
    #   ⚠️⚠️ 最坏后果⛔ 不是话说错了，是**错的窗把它标成已读** ⇒ 真收件人**永远收不到**
    #     （提醒只对未读的信响）。⇒ 必须把抬头亮出来，并明说"不是你就别标已读"。
    抬头 = []
    for n, _, _ in 新:
        m = re.search(r"^收件_[^_]+_给([^_]+)_", n)
        抬头.append("《%s》" % (m.group(1) if m else "看不出抬头"))
    return ("📬 **收件箱有 %d 封没看过的信**（`%s`）：\n%s\n" % (len(新), (d or "?"), 行)
            + "\n".join(尾) + "\n"
            "⚠️ **收件箱是本项目所有窗共用的** ⇒ 上面这些**未必是写给你的**。"
            "抬头分别是：%s\n"
            "⇒ ⭐ **抬头不是你 ⇒ ⛔ 别标已读**（标了真收件人就再也收不到提醒了）；"
            "顺带读一眼没关系。\n"
            "⭐ 确实是给你的 ⇒ 读完移进 `已读/`（`python <垫片目录>/信.py 已读 <文件名>`），"
            "回信用 `信.py 发`——⛔ 别堆着，否则别的窗还会被提醒。\n"
            "⚠️ 信里点名要你改**别人正在动的文件** ⇒ 先查 `git status`＋mtime，"
            "对方还在动就⛔ 只回信、⛔ 不代劳。" % "、".join(抬头))


def _cond_ok(entry, ctx):
    """ctx 里给什么才判得了什么。⛔ 判不了 / 不认识的条件一律 False——
    写错条件名会变成「这条不弹」，⛔ 绝不会变成「无条件弹」（噪音比漏提醒更致命）。"""
    c = entry.get("条件")
    if not c:
        return True
    for name in (str(x) for x in (c if isinstance(c, list) else [c])):
        if name == "本回合无写操作":
            if ctx.get("wrote") is not False:
                return False
        elif name == "本回合改过代码但没跑过":
            if not ctx.get("改码没跑"):
                return False
        elif name == "本项目已配派单":
            if not ctx.get("有派单配置"):
                return False
        elif name == "非codex宿主":
            # ⭐⭐ 2026-08-18 · codex 窗实测抓出来的（⛔ 非设想）：
            #   JF-030（Claude 平台的选档规矩）和 codex 自带的 JF-011（codex 平台的选档规矩）
            #   **在同一次派单里先后各拦了一次** —— 同一件事被拦两遍。
            #   根因：JF-030 靠「本项目未配派单」和 JF-011 互斥，
            #   而 **codex 版的 JF-011 根本没有「条件」字段**（它查的 工作流.yaml 在 codex 上不存在，
            #   所以当初就没给它加）⇒ 那道互斥在 codex 包上**从来没成立过**。
            # ⇒ 本条件表达真正的意思：**这条规矩是给 Claude 平台的，codex 有它自己的那条。**
            # ⭐ 宿主判据只认适配层设的环境变量（见文件头「宿主无关」那段）——
            #   ⛔ 别在这里另发明一套探测。
            if 配置目录 != ".claude":
                return False
        elif name == "本项目未配派单":
            # ⭐ JF-011（体力活派单自查）的**互补条件**，2026-08-18 加。
            #   为什么要这一条：JF-011 只在「本项目已配派单」时响 ⇒ 没配派单的项目
            #   （纯护栏那版插件的典型用户）**一条关于选档的提醒都收不到**
            #   ⇒ 起子代理时默默继承主模型的档，整队按最贵的跑。
            #   ⭐ 两条同一个触发、条件互斥 ⇒ **永不同时响**，⛔ 不是往同一个位置再堆一条分支。
            if ctx.get("有派单配置"):
                return False
        elif name == "本会话未派过单":
            if ctx.get("派过单"):
                return False
        elif name == "非代码文件":
            # ⭐ 2026-08-14 实测标定：JF-017 盯「写进产物的因果断言」，而**代码注释里的因果说明**
            #   （「变化不是操作造成的」这类）是正当解释、⛔ 不是待验证的结论。
            #   866 次真实写入回测：不排代码文件 9 命中（4 条是 蓝湖.py 的注释噪音）⇒ 排掉后 5 条全真。
            if ctx.get("是代码文件") is not False:
                return False
        else:
            return False
    return True


# ---------- transcript 解析（⛔ 不假设 stdin 有现成字段，这是退路也是底座） ----------

def _tail_records(transcript_path, max_bytes=400_000):
    if not transcript_path:
        return []
    try:
        p = Path(transcript_path)
        if not p.exists():
            return []
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if size > max_bytes and lines:
            lines = lines[1:]  # 掉头上那半行
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                if isinstance(r, dict):
                    out.append(r)
            except Exception:
                continue
        return out
    except Exception:
        _log_error()
        return []


def _blocks(rec):
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _block_text(b):
    if b.get("type") == "text":
        return b.get("text") or ""
    if b.get("type") == "tool_result":
        c = b.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(x.get("text") or "" for x in c
                             if isinstance(x, dict) and x.get("type") == "text")
    return ""


def _turn_slice(records):
    """本回合 ＝ 最后一条「真用户消息」（有文本、无 tool_result、非旁线非 meta）之后的记录。"""
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        if r.get("type") != "user" or r.get("isSidechain") or r.get("isMeta"):
            continue
        bs = _blocks(r)
        if not bs:
            continue
        if any(b.get("type") == "tool_result" for b in bs):
            continue
        if any(b.get("type") == "text" and (b.get("text") or "").strip() for b in bs):
            return records[i:]
    return records


def _final_assistant_text(turn):
    for r in reversed(turn):
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        texts = [_block_text(b) for b in _blocks(r) if b.get("type") == "text"]
        joined = "\n".join(t for t in texts if t.strip())
        if joined.strip():
            return joined
    return ""


def _tool_uses(turn):
    out = []
    for r in turn:
        if r.get("type") != "assistant" or r.get("isSidechain"):
            continue
        for b in _blocks(r):
            if b.get("type") == "tool_use":
                inp = b.get("input")
                out.append((b.get("name") or "", inp if isinstance(inp, dict) else {}))
    return out


def _wrote_this_turn(turn):
    for name, inp in _tool_uses(turn):
        if name in WRITE_TOOLS:
            return True
        if name in ("Bash", "PowerShell"):
            cmd = str(inp.get("command") or "")
            if WRITE_CMD_RE.search(cmd):
                return True
    return False


def _changed_code_without_running(turn):
    """本回合改过代码文件、却一次都没真跑过 ⇒ True（JF-006 的条件卫兵）。"""
    改过代码 = False
    跑过 = False
    for name, inp in _tool_uses(turn):
        if name in WRITE_TOOLS and CODE_EXT_RE.search(str(inp.get("file_path") or "")):
            改过代码 = True
        elif name in SHELL_TOOLS:
            cmd = str(inp.get("command") or "")
            if RUN_CMD_RE.search(cmd):
                跑过 = True
            # 用 shell 直接改代码文件（重定向/sed -i）也算改过
            if WRITE_CMD_RE.search(cmd) and CODE_EXT_RE.search(cmd):
                改过代码 = True
    return 改过代码 and not 跑过


def _recent_tool_results(records, n=12):
    """最近 n 条工具结果（时间顺序）。每条 → {"text":…, "fail":bool}。"""
    found = []
    for r in reversed(records):
        if r.get("type") != "user" or r.get("isSidechain"):
            continue
        for b in _blocks(r):
            if b.get("type") != "tool_result":
                continue
            text = _block_text(b)
            # 自指内容（读库日志等）里的 Traceback 字样不算败，⛔ 只认 is_error 旗
            fail = bool(b.get("is_error")) or (
                bool(STRONG_FAIL_RE.search(text or "")) and not _is_self_content(text))
            found.append({"text": text or "", "fail": fail})
        if len(found) >= n:
            break
    found.reverse()
    return found[-n:]


def _consecutive_failures(results):
    k = 0
    for r in reversed(results):
        if r["fail"]:
            k += 1
        else:
            break
    return k


def _stdin_tool_input_text(data):
    """从 stdin 取工具输入（文件路径/命令）；取不到返回空串（走 transcript 退路）。"""
    v = data.get("tool_input")
    if v is None:
        return ""
    try:
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)[-4000:]
    except Exception:
        return ""


# ---------- ⭐ 删除护栏的「自建目录」豁免（用户 2026-08-19 拍板：删自己当轮建的⛔ 不硬拦） ----------
# 病灶（2026-08-18 一天三次真实误拦，⛔ 非设想）：验证脚本自己 mkdtemp 建临时目录、
# 跑完自己 rmtree，三次全被递归删除护栏硬拦——**创建和删除同属一个会话甚至同一条命令**，
# 拦它只产生摩擦，而摩擦最终总是输给「算了，绕过去吧」。
# ⭐ 两条判据，命中任一即视为「自建」：
#   ① 同一条命令里**先创建后删除**（mkdtemp/mkdir/makedirs…出现在删除调用之前）——
#     临时目录的典型形状（路径是运行时生成的，⛔ 只有这条判得了它）；
#   ② 删除目标与**本会话此前 shell 命令里字面创建过的路径**吻合（mkdir X … rm -rf X）。
# ⚠️ 代价如实记：① 是启发式——一条命令建 A 删 B 也会被放过。所以放行**带一行提示**，
#   ⛔ 不是纯静默（沉默不许有两种含义）；且豁免⛔ 不占护栏的冷却次数——
#   放过自建的之后，真删别人的目录**照样硬拦**。
建目录_RE = re.compile(
    r"(mkdtemp\s*\(|TemporaryDirectory\s*\(|makedirs\s*\(|\.mkdir\s*\(|"
    r"\bmkdir\b|^\s*md\s+|New-Item\b[^\n|;]{0,80}?-ItemType\s+Directory)", re.I | re.M)
建目录_取路径_RE = re.compile(
    r"(?:\bmkdir(?:\s+-p)?\s+|^\s*md\s+|makedirs\s*\(\s*r?[\"']|"
    r"New-Item\b[^\n|;]{0,80}?-ItemType\s+Directory[^\n|;]{0,40}?(?:-Path\s+)?)"
    r"[\"']?([^\s\"';|&)]+)", re.I | re.M)
删目录_动作_RE = re.compile(
    r"(\bshutil\.rmtree\s*\(|\brm\s+-[a-zA-Z]*r|Remove-Item\b[^\n|;]{0,80}?-Recurse|\brmdir\s+/[sS])",
    re.I)
删目录_取路径_RE = re.compile(
    r"(?:\brm\s+-[a-zA-Z]+\s+|\bshutil\.rmtree\s*\(\s*r?[\"']|"
    r"Remove-Item\b[^\n|;]{0,60}?|\brmdir\s+/[sS]\s+)[\"']?([^\s\"';|&)]+)", re.I)


def _归一路径(p):
    return str(p).strip().strip("\"'").replace("\\", "/").rstrip("/").lower()


def _记自建目录(cmd, st):
    """PostToolUse：把本会话 shell 命令里**字面创建**的目录路径记进会话状态（去重、封顶 40）。"""
    try:
        库 = st.setdefault("建过的目录", [])
        for m in 建目录_取路径_RE.finditer(cmd or ""):
            p = _归一路径(m.group(1))
            if p and not p.startswith("-") and p not in 库:
                库.append(p)
        del 库[:-40]
    except Exception:
        _log_error()


def _像删自建目录(cmd, st):
    if not cmd:
        return False
    m建 = 建目录_RE.search(cmd)
    m删 = 删目录_动作_RE.search(cmd)
    if m建 and m删 and m建.start() < m删.start():
        return True                      # ① 同一条命令：先建后删
    建过 = set(st.get("建过的目录") or [])
    if not 建过:
        return False
    return any(_归一路径(t) in 建过      # ② 目标是本会话建过的字面路径
               for t in 删目录_取路径_RE.findall(cmd) if t and not t.startswith("-"))
def _accumulate(data, st, 只记派单=False):
    """会话累计（⛔ 与条目命不命中无关——阈值到了才回头看得见前面的量）。

    ⭐ 只算**主窗自己动手**的：stdin 带 agent_id ＝ 这一步是子代理干的（2026-08-13 实测采样确认
      子代理与主窗**共用 session_id**、靠 agent_id/agent_type 区分）⇒ 子代理的写入⛔ 不算主窗下场。

    ⭐⭐ `只记派单=True` 供 **PreToolUse** 用：派单命令要在**发出那一刻**就记下。
    ⚠️ 2026-08-13 实测缺陷：某窗实派 6 张单，`派过单` 仍是 False。只在 PostToolUse 记有三个漏口——
      ① 派单是长任务，那一轮的 PostToolUse 可能因别的异常没跑到；
      ② 会话状态一旦损坏重置，**已经记下的标志会连本带利丢掉**（本批已用原子写＋留证修掉）；
      ③ 后台跑的命令回调形态不保证。
    ⇒ 事前记一次、事后再记一次，**两道都记**（`派过单` 是布尔、`代码文件` 去重 ⇒ ⛔ 不会重复计数）。
    ⚠️ 但代码文件**只在事后记**：事前那一刻写还没发生（可能被拦下或失败），⛔ 不许把"打算写"算成"写了"。
    """
    try:
        if data.get("agent_id"):
            return
        name = str(data.get("tool_name") or "")
        inp = data.get("tool_input")
        inp = inp if isinstance(inp, dict) else {}
        if name in SHELL_TOOLS:
            cmd = str(inp.get("command") or "")
            if not 只记派单:
                _记自建目录(cmd, st)   # ⭐ 只在事后记：事前那一刻目录还没建出来
            if DISPATCH_CMD_RE.search(cmd):
                st["派过单"] = True
            # ⭐ 用 shell 写信也算"我写的"（`cat > x.md`／重定向／Set-Content…）。
            # ⚠️ 2026-08-13 上线首刻实测：只认 Write/Edit ⇒ 我用 heredoc 投完信，
            #   哨立刻提醒我去读**我自己刚写的那封** —— 正是这条卫兵要防的误报。
            #   ⭐ 同一类病（一条腿有一条腿没有）今天第 N 次，⛔ 别只补被点名的那条路径。
            if WRITE_CMD_RE.search(cmd):
                d = _收件目录(data.get("cwd"))
                if d is not None:
                    有 = {p.name.lower() for p in d.glob("*.md")}
                    for m in re.findall(r"[^\s\"'/\\]+\.md", cmd):
                        if m.lower() in 有 and m.lower() not in st.get("收件已看", []):
                            st.setdefault("收件已看", []).append(m.lower())
            # ⭐ 信.py 打印的「已投：<文件名>」＝发信人凭据 ⇒ 记成已看，⛔ 不回头提醒作者。
            #   ⛔ 只认「已投：」这个标记——初稿想扫全部输出里的 .md 文件名，
            #   但 `ls 收件/` 的输出也全是信名 ⇒ 会把**没读过的信**整批误标已看。标记闭合了这个洞。
            resp = _stdin_tool_response_text(data)
            if resp and "已投" in resp:
                d = _收件目录(data.get("cwd"))
                if d is not None:
                    # ⭐⭐ 铃-5（2026-08-17 实测定因）：**原来的字符类会被标题里的「，」截断。**
                    #   ⚠️ 病灶原样记：`收件_[^\s"'，。]+\.md` 把 `，。` 排除在文件名之外，
                    #     而 `信.py` 的标题清洗只滤掉 `:*?"<>|空白#` ——**「，。」是允许进文件名的**。
                    #     ⇒ 标题里带逗号的信，捕获到的名字被**截断** ⇒ `is_file()` 失败
                    #     ⇒ 没记成已看 ⇒ **作者被自己刚投的信提醒**。
                    #   ⚠️ 实证（同一天两封，⛔ 单变量）：
                    #     · 0851 那封标题只有「：＋」⇒ 正常，作者没被提醒；
                    #     · 0918 那封标题含「，」⇒ **作者当场被自己的信提醒**。
                    #   ⭐ 修法：⛔ 不再枚举"文件名里不许有什么"（那份名单必然追不上标题里的字），
                    #     改成**锚到行尾** —— `信.py` 把「已投：<名>」单独打一行，⇒ 行尾是可靠边界。
                    #   ⛔ **「已投」这个标记仍然必须要**：初稿想扫输出里所有 .md，
                    #     但 `ls 收件/` 的输出也全是信名 ⇒ 会把没读过的信整批误标已看。那个洞照旧闭着。
                    # ⭐⭐ 铃-5 真修（2026-08-17 用真采样定的因）：
                    #   Bash/PowerShell 的 `tool_response` 是**字典**，而上面那个取文本的函数
                    #   对非字符串一律 `json.dumps` ⇒ Windows 的 CRLF 被转义成
                    #   **字面的 \r \n 四个字符** ⇒ 正则尾巴的 `\s*$` 遇到的是反斜杠
                    #   ⇒ **零命中** ⇒ 作者照旧被自己刚投的信提醒（生产里实测如此）。
                    # ⚠️ 而我第一版的三条断言**全绿** —— 因为夹具喂的是纯字符串＋真换行，
                    #   生产里两样都不是。⇒ 回归里那组夹具已改用**真采样的形状**。
                    # ⇒ 只在这一处把转义还原，⛔ 不去动 `_stdin_tool_response_text`：
                    #   它的 JSON 化是别的检测器要的（能搜到嵌套字段），⛔ 别为一处改地基。
                    _扁 = (resp.replace("\\r\\n", "\n")
                              .replace("\\n", "\n")
                              .replace("\\r", "\n"))
                    for m in re.findall(r"已投[：:]\s*(收件_.+?\.md)\s*$", _扁, re.M):
                        if (d / m).is_file() and m.lower() not in st.get("收件已看", []):
                            st.setdefault("收件已看", []).append(m.lower())
        elif not 只记派单 and name in WRITE_TOOLS:
            fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
            if fp:
                key = fp.replace("\\", "/").lower()
                # ⭐ 全扩展写入踪迹（撞车卫兵的"这是我自己写的"判据）——⛔ 不限代码文件：
                #   多窗撞车最高发的恰是台账 .md（改动史曾出两对重号：## 86、## 93 各两条）
                st.setdefault("写过", {})[key] = time.time()
                if CODE_EXT_RE.search(fp) and key not in st["代码文件"]:
                    st["代码文件"].append(key)
            # ⭐ 自己写进信箱的信，当场记成"已看" ⇒ ⛔ 不会回头提醒作者自己。
            #   这是把「谁写的谁不审」落在通知层，⛔ 不靠人自觉记得跳过自己那封。
            d = _收件目录(data.get("cwd"))
            if d is not None and fp.lower().endswith(".md"):
                try:
                    if Path(fp).resolve().parent == d.resolve():
                        st.setdefault("收件已看", []).append(Path(fp).name.lower())
                except Exception:
                    pass
    except Exception:
        _log_error()


def _stdin_tool_response_text(data):
    """从 stdin 尽力取工具输出文本；取不到返回空串（然后走 transcript 退路）。"""
    for key in ("tool_response", "tool_result", "tool_output", "output", "response"):
        v = data.get(key)
        if v is None:
            continue
        if isinstance(v, str):
            return v[-20000:]
        try:
            return json.dumps(v, ensure_ascii=False)[-20000:]
        except Exception:
            continue
    return ""


# ⭐⭐ 跨窗那几个工具，两个宿主叫法不同（2026-08-18 · codex 窗在真宿主上实测报回）。
#   ⚠️ 病的样子：上面那些提示词里写死的是 **Claude 的名字**
#     ⇒ codex 上的模型照着去调一个**不存在的工具** ⇒ 白跑一轮还以为自己试过了。
#   ⭐ 只在这儿写一次，⛔ 不在十个提示词里各写两套（那就是本仓的原罪「两本账」）。
本宿主是codex = 配置目录 != ".claude"

CODEX工具对照 = (
    "\n⚠️ **你在 codex 上，上面那几个工具名是 Claude 的，⛔ 这边没有**。换成这三个：\n"
    "  · 列出各窗 ⇒ `list_threads`\n"
    "  · 给某个窗发消息（＝按铃）⇒ `send_message_to_thread`\n"
    "  · 发完要跟进 ⇒ `wait_threads`\n"
    "⛔ **这一格 codex 上没有对应物**：Claude 那边 `get_session` 能拿到对方的\n"
    "  **模型与思考档**（用来判「是不是 Fable、该不该叫醒」）—— codex 这边取不到\n"
    "  ⇒ ⭐ 那一步**跳过**，⛔ 别硬造一个查法，也⛔ 别因此不敢叫。\n"
    "⭐⭐ 但有一条**比 Claude 那边好**（codex 窗实测，⛔ 非推断）：\n"
    "  codex 上给别的任务发消息 **真的会把它启动起来**（发了探针，目标任务真的跑了一轮并回话）。\n"
    "  ⇒ Claude 那边「消息到得了、但叫不醒」的老毛病，**在 codex 上不成立**\n"
    "    ⇒ 上面凡是写着「没在跑就别指望按铃」的话，在这儿**⛔ 不适用**。\n")
RING_TOOL_RE = re.compile(r"send_message$")


def _门铃账本():
    return STATE_DIR / "门铃.jsonl"


def _读门铃(小时=12):
    """读最近 N 小时的门铃流水。⭐ 跨会话共享——调速器要管的是**全局**频率，
    ⛔ 不是单窗自律（单窗各自守规矩，合起来照样能把配额烧光）。"""
    截 = time.time() - 小时 * 3600
    出 = []
    try:
        p = _门铃账本()
        if not p.is_file():
            return 出
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if float(r.get("ts", 0)) >= 截:
                出.append(r)
    except Exception:
        _log_error()
    return 出


def _记门铃(我, 目标, 正文, 流水):
    """把放行的这一次写进门铃流水。⭐ **授权放行的也要记**。

    ⚠️⚠️ 2026-08-16 端到端实测抓出来的：我第一版只在「正常放行」那条路上记账，
      **授权解限那条路忘了记** ⇒ 两个后果：
      ① 授权期内按的铃**事后复盘看不到** —— 而这本流水正是
         「哪些门铃真的改变了对方的下一步动作」的唯一原始数据；
      ② 回球用完、回落到普通闸门那一刻，冷却闸/会话预算**看不到刚才那次**
         ⇒ 第二次按同一个窗**直接放行** ⇒ 「一次性」名不副实。
    ⇒ 两条路共用这一个函数，⛔ 别再各记各的。
    """
    try:
        本会话 = [r for r in 流水 if r.get("从") == 我]
        with _门铃账本().open("a", encoding="utf-8") as f:
            f.write(_可写(json.dumps({
                "ts": time.time(), "t": time.strftime("%Y-%m-%d %H:%M:%S"), "从": 我, "到": 目标,
                "第几次": len(本会话) + 1, "全局第几次": len(流水) + 1,
                "摘要": 正文[:200]}, ensure_ascii=False)) + "\n")
    except Exception:
        _log_error()


def _门铃授权账():
    return STATE_DIR / "门铃授权.json"


def _读门铃授权():
    """读总管授权。⭐ 过期即视为无；**读不出来也视为无**。

    ⚠️⚠️ 「读不出来 ⇒ 视为无授权」这个方向是**故意的**：
      判错方向的后果不对称 —— 误判成"有授权" ⇒ 五道闸全开、无人值守时烧配额；
      误判成"没授权" ⇒ 只是照常受闸门管，最坏是多写几封信。⇒ **往安全的一边错**。
    ⛔ 文件不存在是常态（没人授权过）⇒ 不记错误日志；能读到但读坏了才记。
    """
    p = _门铃授权账()
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        _log_error()                       # ⭐ 有文件却读不出来 ⇒ 这是异常，⛔ 别静默
        return None
    if not isinstance(d, dict):
        return None
    try:
        return d if time.time() < float(d.get("到期时刻") or 0) else None
    except (TypeError, ValueError):
        return None


def _回写门铃授权(目标, 我h, 是回球):
    """放行一次之后记账：已放行次数 +1 · 叫过的窗 append · 回球则计数 +1。

    ⚠️ 这三样都是**读-改-写** ⇒ 必须加锁（多窗同时按铃会互相盖掉）。
    ⭐ 复用 `_账锁` 那份实现，⛔ 不另写一把；但锁的是**授权这本账自己**。
    ⚠️ 拿不到锁**照样往下写**（同 D3 纪律：哨绝不许卡住用户会话）——
      代价是极小概率掉一次计数，⛔ 而不是把会话卡死。
    """
    p = _门铃授权账()
    try:
        with _账锁(账=p) as 锁上了:
            if not 锁上了:
                _log_error()               # ⭐ 掉条要出声，⛔ 别静默（复核 D3 的原话）
            d = json.loads(p.read_text(encoding="utf-8"))
            d["已放行次数"] = int(d.get("已放行次数") or 0) + 1
            叫过 = d.setdefault("叫过的窗", [])
            if 目标 and 目标 not in 叫过:
                叫过.append(目标)
            if 是回球:
                # ⚠️ 授权脚本初始化时**没有**这个键（实测：它只写了 已放行次数/叫过的窗）
                #   ⇒ 这里必须 setdefault，⛔ 不能假设它在
                d.setdefault("回球已用", {})[我h] = int((d.get("回球已用") or {}).get(我h, 0)) + 1
            tmp = p.with_suffix(".tmp")
            tmp.write_text(_可写(json.dumps(d, ensure_ascii=False, indent=1)), encoding="utf-8")
            tmp.replace(p)                 # ⭐ 原子替换，⛔ 别就地覆写（写一半崩了就毁账）
    except Exception:
        _log_error()


def _认领门铃授权(data):
    """PostToolUse：总管窗把自己的 hook 会话号填进授权文件（`待认领` ⇒ false）。
    ⭐⭐ **并在认领的同一刻，把「你现在是总管」当场宣告出去**（返回那段话，⛔ 不再等它按门铃）。

    ⛔⛔ **2026-08-17 用户实测撞出的死锁——⛔ 别把这段宣告去掉**：
      原来总管模式那段话**只挂在「按门铃那一刻」**（`_门铃解限` 的返回值），
      而 `SessionStart` 里**一次都没有**。
      ⇒ ⭐⭐ **死锁**：那个窗**不知道自己是总管** ⇒ 就不会想到去按门铃
        ⇒ **永远收不到那段告诉它「你是总管」的话**。
      ⚠️ 实证：用户授权 C-18 后问它「有没有收到总管窗提示词」——**它答"没收到"**。
        ⛔ 而那**不是触发词不对**，是**时机压根错了**：
        它需要在**开始干活之前**知道自己是总管，⛔ 不是在它已经想到要按门铃的时候。
      ⚠️ 写这段的窗上一轮还向用户断言「你授权的那一刻它会当场收到」——**那句话是错的**。
      ⇒ ⭐ 正解：**认领即宣告**。认领发生在「用户刚跑完授权脚本」那次调用的 PostToolUse
        ⇒ **同一个窗、授权后的第一时间**，⛔ 不需要它做任何额外动作。

    ⭐⭐ 为什么这样就能确定「填的一定是总管窗」：
      授权脚本是**它自己跑的**，而这次 PostToolUse 就是**那次工具调用的回声**
      ⇒ 同一个窗、同一次调用，⛔ 不可能是别人。
    ⚠️ 为什么非得这么绕：hook 的 `session_id` 和 CCD 的 `local_*` 是**两套独立编号**
      （2026-08-13 实测：拿 hook 号去查 CCD 会话查无此会话）
      ⇒ 脚本**没法**把「哪个窗」翻译成 hook 号 ⇒ 只能靠这个时序，⛔ 没有别的办法。
    ⚠️ 本函数每次工具调用都会被调到 ⇒ **没有授权文件时一次 stat 就返回**，⛔ 不做任何读写。
    """
    try:
        p = _门铃授权账()
        if not p.is_file():
            return                      # ⭐ 常态（没人授权过）⇒ 最省的那条路
        授权 = _读门铃授权()
        if not 授权 or not 授权.get("待认领"):
            return
        我h = str(data.get("session_id") or "")
        if not 我h:
            return
        with _账锁(账=p) as 锁上了:
            if not 锁上了:
                _log_error()            # ⛔ 拿不到锁要出声，⛔ 别静默（复核 D3）
            d = json.loads(p.read_text(encoding="utf-8"))
            if not d.get("待认领"):
                return                  # ⭐ 别的调用已经认领过了，⛔ 别覆盖
            d["hook会话"] = 我h
            d["待认领"] = False
            tmp = p.with_suffix(".tmp")
            tmp.write_text(_可写(json.dumps(d, ensure_ascii=False, indent=1)), encoding="utf-8")
            tmp.replace(p)
        _log_trigger("门铃授权认领", "PostToolUse", "总管窗 hook 会话 %s" % 我h[:8], 我h)
        # ⭐⭐ 认领即宣告（见 docstring 里那个死锁）：⛔ 不返回 None，把总管模式那段话交出去
        剩 = max(0.0, (float(d.get("到期时刻") or 0) - time.time()) / 3600)
        # ⭐ `本次算一次=False`：这是**宣告**，⛔ 它并没有在按铃 ⇒ 用量⛔ 不许 +1（假数字）
        return ("✅ **门铃解限已落到你这个窗上**（刚刚认领成功）。\n\n"
                + _总管模式话(d, 剩, "", 本次算一次=False))
    except Exception:
        _log_error()
    return None


def _门铃解限(data, st, 目标, 我h, now, 正文, 流水):
    """总管授权期内的解限判定。返回 (放行吗, 要贴的提醒 或 拦截话)。

    ⭐ 规则来自工作流开发窗 2026-08-16 的终版来信（用户已确认口径）：
      · **总管窗**（用户显式授权、唯一）：任意时间任意次数。
      · **短时转授**（分钟级）/ **实时跟随辅助**（整个任务期，⭐ 同时 ≤1）：按**项目**解限。
      · ⭐ **一次性回球**：目标是总管窗 ⇒ 每个 hook 会话放行 **1 次**。
    ⚠️⚠️ 回球的实现简化（**⛔ 必须原样留着这段说明**）：
      ⛔ 不去核「这个窗到底被总管叫过没有」—— hook 会话号与 CCD `local_*` 是两套编号，
      认不出"我就是被叫过的那个"。⇒ **副作用：没被叫过的窗也能叫总管一次。**
      ⭐ 判为可接受：授权期内总管本来就在收集信息，任何窗有紧急情况能叫它**一次**正合总管模式；
      而「一次」这个硬顶挡住了滥用。
    ⛔ 唯一仍生效的一道：目标是 Fable ⇒ 照拦（总管自己是 Fable 不受此限——
      授权脚本已把它排除在 不叫 名单外）。
    """
    授权 = _读门铃授权()
    if not 授权:
        return False, None
    是总管 = (我h == str(授权.get("hook会话") or "") and not 授权.get("待认领"))
    本项目 = Path(str(data.get("cwd") or ".")).name
    解限项目 = set()
    for t in (授权.get("转授") or []) + (授权.get("跟随") or []):
        try:
            if now < float(t.get("到期时刻") or 0):
                解限项目.add(str(t.get("项目") or ""))
        except (TypeError, ValueError):
            continue
    回球 = (目标 and 目标 == str(授权.get("总管窗") or "")
            and int((授权.get("回球已用") or {}).get(我h, 0)) < 1)
    if not (是总管 or 本项目 in 解限项目 or 回球):
        return False, None

    # ⭐⭐ 口头解限开出来的授权**没有窗清单** ⇒ 判不了目标是不是 Fable。
    #   ⛔ 「判不了」绝不许滑成「不是」——那正是 JF-016（缺失被当成否定）治的病，
    #     而这里滑一次的代价是**真去烧了一个 Fable 窗**，正是用户点名不许的那件事。
    #   ⇒ 只拦**总管往外叫**这一种：从属窗回球的目标是总管，而**总管本来就允许是 Fable**
    #     （用户明说），⛔ 不该被这道连坐。
    if 是总管 and 目标 and not _窗清单已扫(授权):
        return False, _填路径(
            "⛔【授权在，但「该不叫谁」还是空白 ⇒ ⛔ 不敢替你叫】\n"
            "你这份总管授权是**你说一句话就开的**（⛔ 没跑过授权脚本）⇒ **没人扫过各窗用什么模型**\n"
            "⇒ 判不了 `%s` 是不是 Fable，而用户的规矩是「**Fable 不能这么烧**」。\n"
            "⚠️ 「判不了」⛔ 不许当成「不是」——空的不叫名单有两种含义，这里必须问清楚。\n\n"
            "⭐ 补上只要两步，补完这道闸自动放开（之后按铃仍然不限次数）：\n"
            "  ① `list_sessions` 列出各窗，对每个窗 `get_session` 取 `model`\n"
            "  ② `python {纠偏库}/引擎/门铃授权.py 补窗 --窗 <会话号>:<模型名> --窗 …`\n\n"
            "⭐ 只是要告诉对方一件事、⛔ 不需要它立刻改行为 ⇒ **现在就可以写信**"
            "（信不受任何闸限，也不烧对方一轮）。" % 目标[:24], data.get("cwd"))

    # ⭐⭐ 铃-2（2026-08-17 实跑确诊）：「扫过了」是**一个全局布尔**，⛔ 不是逐窗的。
    #   ⚠️ 病灶：补窗只登记了 5 个窗，`窗清单已扫` 就变 True ⇒ 上面那道闸对**所有窗**放开；
    #     而真正的 Fable 检查（下面那条）**只认显式登记在不叫名单里的窗**
    #     ⇒ **没登记过的窗一律免检放行**。实跑证据（⛔ 不是读码推的）：
    #       · 塞一个假的 Fable 窗进清单 ⇒ 按它，**当场拦下**（⭐ 这个绿灯有能力变红）
    #       · 按一个**没登记**的窗 ⇒ **畅通**；拦下它的是 MCP「会话不存在」，⛔ 不是本闸
    #     ⇒ 当时本机 20+ 个窗只登记了 5 个 ⇒ 其余十几个**全在免检状态**。
    #   ⭐ 判据回到 JF-016 那一条：**「没登记」＝「判不了」，⛔ 不许滑成「不是 Fable」。**
    #     ——和上面那道「根本没扫过」同一个道理，只是粒度从**整份授权**降到**单个窗**。
    #   ⚠️⚠️ **作用域故意维持原样：只拦「总管主动往外叫」。** ⛔ 别顺手扩到从属窗——
    #     从属窗回报的目标是总管，而**总管本来就允许是 Fable**（用户明说）；
    #     且总管自己未必在清单里（它读不到自己的 CCD 号，见 D1）⇒ 扩了会**把回报路堵死**。
    #   ⛔ 残留缺口如实记：**被转授的从属窗去叫第三方窗**这一路仍然免检。
    #     ⛔ 没顺手一起堵，是因为堵它要先解决「总管窗认领」（铃-1）——⛔ 不想在没证据的地方多加一条分支。
    if (是总管 and 目标
            and not any(str(w.get("会话") or "") == 目标 for w in (授权.get("窗清单") or []))):
        return False, _填路径(
            "⛔【这个窗**没登记过** ⇒ ⛔ 判不了它是不是 Fable，不敢替你叫】\n"
            "`%s` 不在这次授权登记的窗清单里（清单里有 %d 个）。\n"
            "⚠️ 「没登记」和「登记了、不是 Fable」**⛔ 不是一回事** —— 前者是**判不了**。\n"
            "  而在这儿滑一次的代价是**真去烧一个 Fable 窗**，正是用户点名不许的那件事。\n\n"
            "⭐ 补上只要两步（补完这个窗就通了，⛔ 不影响已登记的窗）：\n"
            "  ① 对它 `get_session` 取 `model`\n"
            "  ② `python {纠偏库}\\引擎\\门铃授权.py 补窗 --窗 <会话号>:<模型名> --窗 …`\n"
            "     ⚠️ **补窗是整份覆盖** ⇒ 把**原来那些也一起再传一遍**，⛔ 别只传新的。\n\n"
            "⭐ 只是要告诉它一件事 ⇒ **现在就可以写信**（信不受闸限，也不烧对方一轮）。"
            % (目标[:24], len(授权.get("窗清单") or [])), data.get("cwd"))

    if any(str(w.get("会话") or "") == 目标 for w in (授权.get("不叫") or [])):
        return False, ("⛔【这个窗是 Fable，⛔ 不叫】`%s` 主模型是 Fable。\n"
                       "⭐ 用户定的：对账叫醒可以不限次数，但 **Fable 不能这么烧**。"
                       "⇒ 改成写信给它。" % 目标[:24])

    _记门铃(我h, 目标, 正文, 流水)      # ⭐ 授权放行的也要进流水，⛔ 别漏（见 _记门铃 的说明）
    _回写门铃授权(目标, 我h, 回球)
    if 回球 and not 是总管:
        return True, ("⭐【一次性回球】你正在回报总管——这是授权期内**给你的唯一一次**。\n"
                      "⇒ 用完了 ⇒ 之后有事**写信**（信落盘不会丢，门铃会丢）。" + 送达警句)
    剩 = max(0.0, (float(授权.get("到期时刻") or 0) - now) / 3600)
    # ⚠️⚠️ 括号别省：`A if c else B + 尾` 会被读成 `A if c else (B + 尾)`
    #   ⇒ **总管那一支拿不到尾巴**。⛔ 这不是假设——2026-08-17 第一版就是这么写的，
    #   靠 铃-6 那条断言当场红出来的（⭐ 而四条「该通/该拦」全绿，⛔ 一条都没照到它）。
    return True, ((_总管模式话(授权, 剩, 目标) if 是总管 else
                   ("⭐【解限中·从属窗】%s｜还剩 %.1f 小时。\n"
                    "⇒ 按之前答一句：**这条是必须现在敲的吗？还是写信更合适？**"
                    % (str(授权.get("事由") or "（没写事由）"), 剩))) + 送达警句)


# ⭐⭐⭐ 铃-6（2026-08-17 两次实测事故）：**「送达成功」是个假象，必须当场说破。**
#
# ⚠️ 病的形状：发起方这边**每一个信号都是绿的** ——
#   `send_message` 当场返回「已送达」、账本 `已放行` +1、提示词照常贴出；
#   而对面可能：① 进程直接死了（打包发布窗：唤醒后 18 分钟零产出，前端报 stopped responding）
#              ② **轮次根本没建立**（本窗：那个时段转录里 **0 条记录**，消息从没变成一轮对话）
#   ⇒ 发起方**完全分不出**「在忙 / 在压缩 / 死了 / 根本没收到」——沉默有四种含义。
#
# ⭐ 本仓的判据：**沉默⛔ 不许有两种含义。** 投递这一段在宿主里，⛔ 我们改不了；
#   ⇒ 那就**改我们改得了的那半**：把「这条可能会丢」写在放行的那一刻，
#     并给出**不会丢的替代**（写信落盘）。⛔ 不许让人以为按出去就等于送到了。
#
# ⚠️ 当天实测比分（⛔ 别删，这是这段文案唯一的依据）：
#   按铃**唤醒** 0/3 成功（⭐ 但消息**到达**是好的：3/3 都出现在对方窗口里）·
#   用户手动唤醒 3/3 成功 · 写信 4/4 送达 · 扫转录全部拿到。
# ⚠️⚠️ **⛔ 别把这条归因成「睡着的窗才这样」** —— 那个归因被 10:20 那封红信**自己撤回**了：
#   唯一一次「目标醒着」的实验其实没成立（消息排在对方整份报告之后 ⇒ 它当时闲着）
#   ⇒ **「醒着/睡着」这个变量至今没被翻转过** ⇒ 两种解释都活着，⛔ 此处不许选一个。
送达警句 = ("\n⚠️ **「已送达」⛔ 不等于「对方会读到」**：当天实测 **3 次按铃、0 次真的唤醒对方**。\n"
            "  ⭐ 消息**确实到了**对方窗口（看得见），但它**⛔ 不会启动对方的一轮**\n"
            "  ⇒ 对方要等**下一次被人叫起来**才看得到；而你这边**全是绿的**（返回已送达／账本+1）。\n"
            "  ⚠️ 送达那一刻**护栏的挂点一次都没跑**（有心跳对照：前后手打的消息都记下了，门铃夹在中间没有）。\n"
            "⇒ ⭐ 要紧的内容**同时写一封信**（信落盘不会丢）；"
            "⛔ 别把只存在于门铃里的话当成对方已经知道了。")


# ⭐⭐ 「总管模式」的正文（用户 2026-08-17 亲自校过措辞，⛔ 别改回禁令腔）
#
# ⚠️⚠️ **第一版被用户当场否掉，那次否定是本段最重要的资产**：
#   我原本写「⛔ 不猜、⛔ 不自己起真空环境」。用户原话：
#   「那个"不猜"和"不自己起真空环境"会不会限制有点大了，**有时可能确实需要它临时起真空环境验证**，
#     **猜本身也是 AI 的判断能力**，我要的是它**在猜之前先想办法找到能证实部分猜测的东西**
#     （比如找做过这层的窗口来问当时实现过的需求和调整过程，**并要求对方自检然后呈上供词**），
#     而不是"这块大概是上一窗口做错了"这种**盲猜**——总之既然是 Agent 了，
#     **就要少限制多促进，多提醒它还有什么选择/工具，而不是一直告诉它什么不能做**。」
#
# ⇒ ⭐ 两条落到写法上的硬要求：
#   ① **区分的不是"猜不猜"，是"取证了没有"**——盲猜（张口就下判断）⛔；
#      先取证、再在证据上推断 ✅。⭐ 起真空环境**是正当工具**，⛔ 不是禁忌。
#   ② **主体是"你有哪些工具"，⛔ 不是"你不许做什么"** ——
#      本段全文只有一处 ⛔（盲猜那一处），其余全是**能力清单**。⭐ 改它的人请守住这个比例。
# ⭐⭐ 「先扫一眼谁在跑」这一段，两个宿主的**工具名和结论都不同** ⇒ 整段按宿主二选一。
#   ⚠️⚠️ ⛔ 别改成「Claude 版正文 ＋ 末尾加一段 codex 更正」——那样旧名字仍留在正文里，
#     模型很可能**先照着旧名字去调**，撞一次不存在的工具才读到更正。⭐ 换掉，⛔ 不是补丁。
#   （2026-08-18 · codex 窗在真宿主上实测报回：名字不同、且「叫不叫得醒」结论相反。）
扫窗段_claude = (
    "⭐⭐ **要别的窗配合之前，先扫一眼谁在跑**（用户 2026-08-17 点名要的一步）：\n"
    "  ⇒ `list_sessions` 就带着每个窗的 **isRunning · title · lastActivityAt**；\n"
    "     `get_session` 还能拿到 **model · effort**（⇒ 顺带就把 Fable 那条判了）。\n"
    "  · **在跑** ⇒ ⭐ **直接按铃，不限次数**"
    "（平时那 4 次上限、同一个窗 20 分钟的冷却，期内都关着）\n"
    "    ⇒ 实时联调／对齐**尽管按** —— 这正是这份授权买来的东西。\n"
    "  · **没在跑** ⇒ ⭐ 走这两步更管用：实测按铃**⛔ 不会启动它的一轮**\n"
    "    ——消息**到得了**（对方窗口里看得见），但它要等**下一次被人叫起来**才读到。\n"
    "    ⇒ ⭐ **先写信**（内容落盘不会丢）＋ **把一条现成的唤醒指令贴给用户**，\n"
    "      请他手动粘进那个窗。⇒ 生成那条指令：\n"
    "      `python {纠偏库}\引擎\门铃授权.py 唤醒词 --给 <角色> --信 <信的路径>`\n"
    "  · 这活**要隔离／要独立复核／要掺错实跑** ⇒ ⭐ **建议用户新开一个窗**\n"
    "    （⭐ 新窗＝干净上下文；复用旧窗等于把一段脏上下文带进一件新事）。\n"
    "  ⚠️ 用户实测提醒：那条唤醒指令**通宵作业时仍然经常挂掉** ⇒ ⛔ 别把它当成万无一失，\n"
    "    要紧的东西**永远同时落在信里**。\n")

扫窗段_codex = (
    "⭐⭐ **要别的窗配合之前，先扫一眼谁在跑**：\n"
    "  ⇒ `list_threads` 列出同工作区的各个任务；发消息用 `send_message_to_thread`，\n"
    "     发完要跟进用 `wait_threads`。\n"
    "  ⛔ **有一格这边没有**：Claude 那边能查对方的**模型与思考档**（用来判「是不是太贵的档、\n"
    "    该不该叫醒」）—— codex 这边取不到 ⇒ ⭐ 那一步**跳过**，\n"
    "    ⛔ 别硬造一个查法，也⛔ 别因此不敢叫。\n"
    "  ⭐⭐ **这边比 Claude 那边好的一点**（实测，⛔ 非推断）：\n"
    "    给别的任务发消息 **真的会把它启动起来** —— 发过探针，目标任务真的跑了一轮并回话。\n"
    "    ⇒ Claude 那边「消息到得了、但叫不醒」的老毛病，**在这儿不成立**\n"
    "    ⇒ 所以⛔ 不必先分「在跑／没在跑」再决定敢不敢按 —— **该问就问**。\n"
    "  · 这活**要隔离／要独立复核／要掺错实跑** ⇒ ⭐ **建议用户新开一个任务**\n"
    "    （⭐ 新任务＝干净上下文；复用旧的等于把一段脏上下文带进一件新事）。\n"
    "  ⚠️ 要紧的东西**永远同时落在信里** —— 落盘不会丢，⛔ 别只靠一次消息。\n")
def _总管模式话(授权, 剩, 目标, 本次算一次=True):
    """⭐ `本次算一次`：只有**这次真的在按铃**时才 +1。

    ⛔⛔ 2026-08-17 实测抓出的假数字：认领宣告和开窗重提**都没有在按铃**，
      却照样印「已按 1 次」——⭐ 那是**编出来的**。本仓不许留假数字（同「裸阈值」那条纪律）：
      ⚠️ 一个连自己用量都报不准的提示，凭什么让人相信它别的数？
    """
    跟 = [t.get("项目") for t in (授权.get("跟随") or [])
          if time.time() < float(t.get("到期时刻") or 0)]
    return ("⭐⭐【你现在是本次任务的总管】事由：%s｜授权还剩 **%.1f 小时**｜"
            "已按 %d 次、叫醒过 %d 个窗%s\n"
            "\n"
            "⇒ ⭐ **目标达成之前，推进由你负责**：拆下一步 → 缺什么就去取 → 拿到就往下推。\n"
            "  ⭐ 只在**真需要价值判断**（要不要做、优先级、验收标准）时回来找用户。\n"
            "\n"
            "⭐ **你手上有这些**（⇒ 信息不全时，⭐ 先从这里挑一个，⛔ 别张口就下判断）：\n"
            "  · **扫别人的转录**——答案常常已经在某个窗的推理过程里，⭐ 扫到就不用打扰人。\n"
            "  · **派子代理／codex 去抓某个窗的上下文原文**（用户 2026-08-17 定的口径）。\n"
            "  · **要它干活 ⇒ ⭐ 起一个新会话最稳**——旧窗上下文又长又脏，\n"
            "    让它干一件毫无准备的新事，当天崩过一次（18 分钟零产出后进程死）。\n"
            "  · ⭐⭐ **按铃找正在跑的窗——授权期内不限次数**\n"
            "    （平时是**每会话 4 次**、同一个窗 **20 分钟**才能按一次；期内这两道都开着）。\n"
            "    ⭐ 这是本次解限**最实的那一块**：几个窗一起推一件长任务时，\n"
            "      **实时联调／对齐本来就不够按** —— 用户就是撞在这个上限上才提的这套东西。\n"
            "    ⚠️ 边界只有一条：**对方得在跑**（不在跑的怎么办，见下一段）。\n"
            "    ⇒ 问的时候别只问结论，**要它自检、把证据呈上来**（当时的需求是什么、\n"
            "      改过哪些地方、凭什么认为当时是对的）——⭐ 供词比结论有用得多。\n"
            "  · **给某个窗临时解限**，让它当下手替你查／跑（`门铃授权.py 转授`）。\n"
            "  · **给一个窗整个任务期解限**当实时助手（`跟随`，⭐ 同时只许一个）。\n"
            "  · **自己起一个隔离环境去验**——⭐ 这是**正当手段**，"
            "尤其「只想确认某个猜测成不成立」时最快。\n"
            "  · **写信**（落盘不会丢）＋ **待应答巡检** 看谁还没回你。\n"
            "\n"
            "%s"
            "\n"
            "⭐ 判据只有一条：**推断可以，但推断得站在你取到的东西上。**\n"
            "  ⇒ 「这块大概是上一个窗做错了」——⛔ 这种**没取过任何证就下的判断**才是要避免的；\n"
            "  ⇒ 「我扫了它的转录 ＋ 叫它自检交了供词，据此判断是那一处」——⭐ 这就是对的。\n"
            "\n"
            "⚠️ 期内五道闸全关（含防「互相按到天亮」的乒乓闸）⇒ ⭐ 按之前顺带想一下：\n"
            "  **这条是必须现在敲的吗？还是写信更合适？**（⛔ 只是提醒，⛔ 不拦你）"
            % (str(授权.get("事由") or "（没写事由）"), 剩,
               int(授权.get("已放行次数") or 0) + (1 if 本次算一次 else 0),
               len(set(授权.get("叫过的窗") or []) | ({目标} if 目标 and 本次算一次 else set())),
               ("｜⭐ 实时助手：%s" % "、".join(跟)) if 跟 else "",
               # ⭐ 这一格按宿主二选一（名字与「哪一格没有」的唯一真源在上面那两个常量）
               扫窗段_codex if 本宿主是codex else 扫窗段_claude)
            )


# ---------- ⭐⭐ 说出「解限」即拿到身份（用户 2026-08-17 第三次点名：实测仍未实现） ----------
#
# ⭐⭐ 为什么必须挂在 **UserPromptSubmit** 上（⛔ 别再挪回别的挂点）：
#   用户原话：「**当我说出"解限"，你就能知道自己的身份，拿到那段总管模式提示词**，
#             知道自己能干什么该干什么。」
#   ⇒ 「说出」这件事发生在**用户输入**里，而本次改动之前，四个挂点
#     （开窗／工具输入／工具输出／助手回复）**没有一个读得到用户输入**
#     ⇒ 这条需求在物理层面就不可能成立，⛔ 不是措辞或时机没调好。
#
# ⚠️⚠️ 上一轮修的两层**都没解开死锁，只是把它挪了个地方**（⛔ 别再重复这个错）：
#   · 「认领即宣告」要求**先有人跑授权脚本**；· 「开窗重提」要求**授权文件已经存在**。
#   ⇒ 而「谁去跑那个脚本」正是死锁本身：它不知道自己是总管 ⇒ 不会去跑 ⇒ 永远不被告知。
#   ⇒ 死锁从「按门铃才知道」挪到了「跑脚本才知道」，用户实测一次就撞穿了。
#   ⭐ 这两层**照旧留着**：它们覆盖「用户自己跑脚本授权」和「授权后关窗又开」两条路，
#     本节覆盖的是第三条——**用户就在这儿说了一句话**。三条互补，⛔ 不重复。

解限默认小时 = 10.0          # ⭐ 用户原话「长达十小时」；与 门铃授权.py 的硬上限同源

# ⭐ 强句式。⚠️ 改它之前先读下面「两道尺子」——正则本身⛔ 不是防线。
_授权句 = re.compile(
    r"(解除|解开|放开|取消|去掉)\s*(一下|了)?\s*(门铃|按铃)?\s*(限制|闸|配额|上限)"
    r"|(门铃)?\s*解限"
    r"|(改为|转为|进入|激活|开启|启用|切到|切换到|开始)\s*总管模式"
    r"|你\s*(现在|从现在起|这次|本次|接下来)?\s*(就)?是\s*(本次任务的|这次的|本轮的|本次)?\s*(总管|主窗)"
    r"|授权\s*你\s*(当|做|作为|为)?\s*(总管|主窗)"
    r"|(给|对)\s*你\s*解限")

_撤权句 = re.compile(
    r"(撤销|撤掉|收回|解除|结束|关闭|退出|停用)\s*(一下)?\s*(总管模式|总管授权|总管身份|解限|门铃解限)"
    r"|总管模式\s*(到此)?\s*(结束|收工|撤销|停)")

# ⭐⭐ **两道尺子**（⛔ 正则不是防线，这两个数才是）：
#   ⚠️ 真实的误判来源⛔ 不是「正则写歪」，是**用户把需求文档整段粘进来**——
#     那份文档里「解限」「总管模式」出现十几次，而它⛔ 不是授权，是在**描述**授权。
#     （2026-08-17 本窗开工时用户粘的那份自呈就是这样，⇒ 这不是假设，是眼前的样本。）
#   ⇒ 判据⛔ 不能只看词，要看**这句话的形状**：真授权是一句**短命令**；描述是**长句**。
#   ⭐ 切句时**故意不切逗号**——「我不止希望门铃解限，」这种从句切出来会短得像命令，
#     那正是误判的来源。只按换行与句末标点切。
授权短句上限 = 40            # ≤ 这个长度 ⇒ **当场落授权**
授权疑似上限 = 160           # 40~160 ⇒ ⛔ 不落账，只问一句（⛔ 沉默不许有两种含义）


def _句子们(text):
    return [s.strip() for s in re.split(r"[\n。！？!?；;]+", text or "") if s.strip()]


# ⭐⭐⭐ 授权口令（2026-08-17 用户拍板：**收窄成一句固定指令**）
#   用户原话：「收窄成一句固定指令吧，这个**不能随便解**，**设宽了误触发没意义**。」
#   ⚠️ 触发这次收窄的真实事故：用户写「1.别漏了从属窗对自己的权限可见性（被解限）的需求」
#     ⇒ 旧判据（6 种句式 ＋「≤40 字算下令」）当场判成授权，**自动开了 10 小时、五道闸全关**。
#   ⭐ 病根⛔ 不在正则写得好不好，在**判据本身是概率性的**：
#     「解限」「总管模式」这些词**天然出现在讨论它们的文档里**（用户当天粘的需求自呈里十几次），
#     旧版靠「句子长度」躲过去 —— 那是启发式，⛔ 不是保证。
#   ⭐ 而错法是**不对称**的 ⇒ 判据必须往**漏判**那边偏：
#     · 漏判（真授权没认出来）⇒ 用户再说一遍，代价一句话；
#     · 误判（把讨论当成授权）⇒ **五道闸全开 10 小时**，且当事人**不知道自己被解限了**。
授权口令 = "解除门铃限制，进入总管模式"
撤销口令 = "结束总管模式"


def _口令归一(s):
    """比对前的归一：去空白、统一中英标点、削掉句尾语气符。

    ⛔ **只做这些**——⛔ 别顺手加「去掉所有标点」之类的宽松化：
      每放宽一点，就把「整句相等」往「差不多就算」上拉一步，而那正是本次要消掉的东西。
    """
    t = re.sub(r"\s+", "", str(s or ""))
    t = t.replace(",", "，").replace(".", "。").replace("．", "。")
    return t.rstrip("。！!?？~～、")


def _解限意图(prompt):
    """返回 ('授权'|'撤销'|'疑似'|None, 命中的那句)。

    ⭐⭐ **授权只认整句相等**（⛔ 不是"包含"、⛔ 不是"以它开头"）：
      整条 prompt 归一后必须**恰好等于** `授权口令`。
      ⇒ 这一条把「粘进来的文档里出现这句话」这类误判**从结构上**消掉：
        文档里那句话周围一定还有别的字 ⇒ 整句不相等 ⇒ ⛔ 不触发。
      ⛔ 别退回「包含」或「开头」——那两种都还给误判留着门。

    ⚠️ **撤销故意保留旧的宽判据**（⛔ 不是漏改）：错法在这一侧是**反过来**的 ——
      误撤 ⇒ 闸门恢复（安全方向）；漏撤 ⇒ 五道闸继续全关（危险方向）。
      ⇒ 撤销宁可**多认**：固定口令 **或** 旧句式，命中任一就撤。

    ⭐ 「疑似」这一档判据可以宽，因为它**⛔ 不产生任何后果**，只贴一句提示
      ⇒ 收窄之后仍然⛔ 不会出现「用户说了却完全没反应」（沉默不许有两种含义）。
    """
    原 = str(prompt or "")
    净 = _strip_quoted(原)
    if _口令归一(原) == _口令归一(撤销口令):
        return "撤销", 撤销口令
    if _口令归一(原) == _口令归一(授权口令):
        return "授权", 授权口令
    for s in _句子们(净):
        if len(s) <= 授权疑似上限 and _撤权句.search(s):
            return "撤销", s
    for s in _句子们(净):
        if len(s) <= 授权疑似上限 and _授权句.search(s):
            return "疑似", s
    return None, ""


def _窗清单已扫(授权):
    """这份授权到底扫没扫过各窗模型。

    ⭐ 缺键时按「有没有窗清单」推断——脚本申请的老授权本来就强制带清单（⛔ 不带就拒绝授权），
      所以「有清单 ＝ 扫过」对旧文件成立。
    """
    if "窗清单已扫" in 授权:
        return bool(授权.get("窗清单已扫"))
    return bool(授权.get("窗清单"))

# ⭐⭐ 夜-3（2026-08-18）：用户说「我要睡了」⇒ 当场给睡前清单。
#   ⚠️ 触发⛔ 不学解限那样「整句相等」——那对这件事太严。两者代价差着数量级：
#     解限误触 ⇒ **自动开 10 小时、五道闸全关**（2026-08-17 真出过事故）；
#     本条误触 ⇒ **最多多贴一段清单**。⇒ ⭐ 所以这里可以宽一点。
#   ⛔ 但仍要窄到不会在**讨论**睡觉时误响 ⇒ 加一条硬门槛：**整条消息 ≤ 24 字**。
#     （宣告「我要睡了」很短；讨论「昨晚我要睡了的时候那个窗挂了…」必然长）
睡前词 = ("我要睡了", "我去睡了", "我睡了", "我先睡", "睡觉去了", "我挂机了",
          "我下班了", "我要挂机", "去睡了")
睡前最长 = 24        # ⚠️ 24 是**拍的**：够放「我要睡了，你们自己推」这类整句，
                     #   ⛔ 无实测依据 —— 真误触/漏触后按实例回校。


def _睡前意图(prompt):
    """整条消息够短 ＋ 命中睡前词 ⇒ True。⛔ 长句一律不算（那是在讨论，不是在宣告）。"""
    文 = (prompt or "").strip()
    if not 文 or len(文) > 睡前最长:
        return False
    return any(w in 文 for w in 睡前词)


def _睡前话(cwd):
    """⛔ 这里只给「去哪拿」和「模型必须补什么」，⛔ 不在 hook 里算清单——
    hook 是同步的、跑在用户每一轮输入上，⛔ 不该去跑 git / 扫信箱。"""
    return _填路径(
        "🌙 **听起来你要去睡/挂机了 —— 先把睡前这几件过一遍。**\n"
        "\n"
        "⭐ **第一步，先跑这条**（它会把该看的都摊出来）：\n"
        "   `python {纠偏库}\\引擎\\夜班.py 交接`\n"
        "\n"
        "⚠️ 那条命令**只查得到机械的那半**。⭐ 你（模型）必须**另外补两段**，"
        "⛔ 别让用户以为脚本那份就是全部：\n"
        "  ① **此刻各窗在跑什么** —— `list_sessions`（脚本调不到，只有你能拿）\n"
        "  ② **今夜要谁干什么** —— 落一份交接单：\n"
        "     `夜班.py 交接单 --目标 … --判据 … --停手 …`（三样缺一个它会出声）\n"
        "\n"
        "⭐ **要有窗夜里干活 ⇒ 现在就让用户把它喊起来**："
        "实测**睡着的窗按铃叫不醒**（7 次 0 成）⇒ 他一睡，就没人能叫它了。\n"
        "\n"
        "⚠️ **⛔ 别承诺夜里出事会有人知道** —— 除非你真的布了防并**回读到心跳**"
        "（`夜班.py 布防`；守望要用 Monitor 挂）。⛔ 「装了哨兵」≠「有人在盯」。",
        cwd)


def _落总管授权(data, 事由句):
    """用户当场说了句解限 ⇒ 把授权**直接落到本窗**。返回 (授权dict 或 None, 是否接管了别人的)。

    ⭐ ⛔ 不留「待认领」：脚本那条路要待认领，是因为脚本不知道自己的 hook 会话号；
      而本函数就跑在**那个窗的 UserPromptSubmit 里** ⇒ `session_id` 就在手上 ⇒ 直接钉死。
    ⚠️⚠️ **窗清单是空的**——用户只说了一句话，没人扫过各窗用什么模型。
      ⛔ 「不叫名单为空」在这里**绝不许**被读成「没有 Fable 窗」（那正是 JF-016 治的病：
      拿不到信息时默认落进一个具体结论）⇒ 落一个显式的 `窗清单已扫: False`，
      按铃那一刻据它拦（见 `_门铃解限`）。⭐ 于是「判不了」是**机械可见**的，⛔ 不靠谁记得。
    """
    我h = str(data.get("session_id") or "")
    if not 我h:
        return None, False
    now = time.time()
    旧 = _读门铃授权()
    接管 = bool(旧 and str(旧.get("hook会话") or "") not in ("", 我h))
    d = {"事由": (事由句 or "").strip()[:60] or "用户当场解限（没写事由）",
         "授权时刻": now, "到期时刻": now + 解限默认小时 * 3600, "小时": 解限默认小时,
         "总管窗": "", "总管是禁叫模型": False,
         "hook会话": 我h, "待认领": False,
         "窗清单": [], "不叫": [], "窗清单已扫": False,
         "开法": "用户口头解限（UserPromptSubmit）",
         # ⭐ 这五个键在这里就落全 —— 插件窗 2026-08-16 的话：「少一个键和值为空是两回事，
         #   下一个读它的人未必像我这样处处 setdefault」。
         "转授": [], "跟随": [], "已放行次数": 0, "叫过的窗": [], "回球已用": {}}
    p = _门铃授权账()
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_可写(json.dumps(d, ensure_ascii=False, indent=1)), encoding="utf-8")
        tmp.replace(p)                 # ⭐ 原子替换，⛔ 别就地覆写
    except Exception:
        _log_error()
        return None, False
    return d, 接管


def _门铃调速器(data, st):
    """PreToolUse 拦 `send_message`：门铃是**唯一会花掉对方配额**的动作 ⇒ 必须有闸。

    ⭐⭐ 立此闸的场景（用户 2026-08-13 睡前明令）：三个窗挂着互相按门铃跑一夜，
      「回执→回执」这种礼貌性往返会**无限增殖**，而每一次都在对方那边烧一整轮
      （⚠️ 对方主模型若是贵档，代价成倍）。⛔ 靠"大家自觉别乱按"必然失效——今天已经证过太多次。

    四道闸，⛔ 每道都对应一种真实烧法：
      ① **乒乓**：对方 30 分钟内按过我 ⇒ ⛔ 不许回按。回信落盘即可，对方自己的拉取会看见。
         （这是死循环的唯一入口，堵住它就不会失控。）
      ② **同一目标冷却 20 分钟**：连着捅同一个窗＝把它的注意力打碎，⛔ 与"必要"无关。
      ③ **本会话预算**（缺省 4 次/会话）：逼发信人排序——⛔ 什么都按＝什么都不重要。
      ④ **全局 12 小时上限**（缺省 12 次）：无人值守时的总闸，⛔ 单窗守规矩也挡不住三窗合力。

    ⭐ 放行的那次会**记账**（谁→谁、理由、第几次）——理由字段是明天复盘
      「哪些门铃真的改变了对方的下一步动作」的原始数据。⛔ 不记账就永远说不清什么是"必要"。
    """
    if data.get("agent_id"):
        return None
    tool = str(data.get("tool_name") or "")
    if not RING_TOOL_RE.search(tool):
        return None
    inp = data.get("tool_input")
    inp = inp if isinstance(inp, dict) else {}
    目标 = str(inp.get("session_id") or "")
    正文 = str(inp.get("message") or "")
    我 = str(data.get("session_id") or "?")
    now = time.time()

    # ⭐⭐ 总管授权解限：判在**五道闸之前**（授权期内它们全关，含乒乓闸）。
    #   ⚠️ 顺序要紧——放在后面就等于「先被闸拦住再说」，授权就白给了。
    流水 = _读门铃()
    _门铃调速器.本次提醒 = None
    放行, 话 = _门铃解限(data, st, 目标, 我, now, 正文, 流水)
    if 放行:
        _门铃调速器.本次提醒 = 话             # ⭐ 放行时贴的提醒由 PreToolUse 出口统一带出
        return None
    if 话:
        return 话                            # ⛔ 唯一仍生效的一道：目标是 Fable

    cfg = {}
    try:
        f = Path(data.get("cwd") or ".") / 配置目录 / "工作流.yaml"
        if f.is_file():
            import yaml
            cfg = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("门铃") or {}
    except Exception:
        pass
    乒乓分 = int(cfg.get("乒乓静默分钟", 30))
    冷却分 = int(cfg.get("同目标冷却分钟", 20))
    会话上限 = int(cfg.get("每会话上限", 4))
    全局上限 = int(cfg.get("十二小时上限", 12))

    # ⭐⭐ 第 ⓪ 闸 · 成本不对称（2026-08-13 实测确立，⛔ 这条比后面四条都根本）：
    #   框架里**读对方 transcript 是免费的**（`list_events` / `search_session_transcripts`
    #   / `get_session`——对方那边**不消耗任何一轮**），而门铃**连对方那一轮都不会启动**
    #   （⚠️⚠️ 2026-08-17 实测换掉了本闸的论据，**而结论更硬**：3 次按铃、0 次真的唤醒；
    #     消息到得了、看得见，但⛔ 不启动对方的回合 ⇒ 拿门铃问信息是**双重无用**：
    #     既拿不到答案，又让你误以为已经问过了）
    #   （⚠️ 对方主模型若是贵档，代价成倍）。
    #   ⇒ 铁律：**凡是"我想知道对方在干什么/结论了什么"的，一律去读，⛔ 永远不许按铃问。**
    #     门铃只配用于「**对方需要改变行为**」——⛔ 不是「我需要信息」。
    #   ⚠️ 正则只抓得住"问句形态"这一层表面信号；真正的判据是那句自问：
    #     **我是要它改变行为，还是我自己缺信息？** 后者 ⇒ 去读。
    问句 = re.compile(r"(在干(什么|嘛)|进展如何|怎么样了|有没有(查|做|跑)|能不能告诉|你那边.*(情况|结论|数据)"
                      r"|同步一下|对齐一下|确认一下你|请回复|收到吗|读了吗|\?$|？$)")
    if 问句.search(正文[:400]):
        return ("⛔【门铃调速器 · 成本不对称闸】这条门铃看起来是在**问对方要信息**。\n"
                "⭐ 框架里**读对方 transcript 是免费的**（`list_events` 读它最近在干什么、"
                "`search_session_transcripts` 全文搜哪个窗碰过某文件/某报错、`get_session` 看它模型档位）"
                "——**对方那边不消耗任何一轮**；而按铃**连它那一轮都不会启动**（2026-08-17 实测：3 次按铃、0 次唤醒 ⇒ 你问了也等不到答案）。\n"
                "⇒ 先去读。读完仍需要它**改变行为**（停下／换做法／别提交），那时再按铃，"
                "并在正文里写明「我预期你因此改变什么」。\n"
                "⛔ 门铃⛔ 不是问询工具，它是打断工具。")
    近 = [r for r in 流水 if r.get("从") == 目标 and r.get("到") == 我
          and now - float(r.get("ts", 0)) <= 乒乓分 * 60]
    if 近:
        return ("⛔【门铃调速器 · 乒乓闸】`%s` 在 %d 分钟内**按过你**——回按它就是死循环的第一环。\n"
                "⇒ 回复请**只写信**（`信.py 发`）：对方的哨会在它下一次工具调用时看见，⛔ 不会漏。\n"
                "⭐ 门铃只用于「**对方不知道、且知道得越早损失越小**」的事；"
                "回执/确认/致谢**永远⛔ 不配按铃**。" % (目标[:24], 乒乓分))
    同 = [r for r in 流水 if r.get("到") == 目标 and now - float(r.get("ts", 0)) <= 冷却分 * 60]
    if 同:
        剩 = int(冷却分 - (now - float(同[-1]["ts"])) / 60)
        return ("⛔【门铃调速器 · 冷却闸】你 %d 分钟前刚按过 `%s`，还剩 %d 分钟冷却。\n"
                "⇒ 连着捅同一个窗＝把它的注意力打碎，⛔ 这与「必要」无关。\n"
                "⇒ 新情况**并进一封信**；真到了「不立刻说它就会白干」的程度 ⇒ 等冷却到点再按。"
                % (冷却分 - 剩, 目标[:24], max(1, 剩)))
    本会话 = [r for r in 流水 if r.get("从") == 我]
    if len(本会话) >= 会话上限:
        return ("⛔【门铃调速器 · 会话预算】本会话已按 %d 次门铃（上限 %d）。\n"
                "⇒ 预算用完⛔ 不是「不许沟通」——**写信不受任何限制**，只是不再主动打断别人。\n"
                "⭐ 这条闸的用意：逼你排序。什么都按＝什么都不重要，而每次按铃都在花对方的配额。"
                % (len(本会话), 会话上限))
    if len(流水) >= 全局上限:
        return ("⛔【门铃调速器 · 全局闸】最近 12 小时全部窗口共按了 %d 次门铃（上限 %d）。\n"
                "⇒ ⚠️ 无人值守时段的总闸：单个窗各自守规矩，**三个窗合力照样能烧光配额**。\n"
                "⇒ 改为写信；要抬高上限得用户点头（`.claude/工作流.yaml` 的 `门铃:` 节）。"
                % (len(流水), 全局上限))
    _记门铃(我, 目标, 正文, 流水)      # ⭐ 与授权那条路共用同一个记账函数，⛔ 别各记各的
    # ⭐⭐ 铃-10（2026-08-17 深夜发现）：**普通放行也要带送达警句。**
    #   ⚠️ 原来它只挂在「有授权」那条路上 ⇒ **平时按铃一次都看不到它**，
    #     而「已送达⛔ 不等于对方会读到」跟有没有授权**毫无关系**
    #     （当天 4 次失败没有一次依赖授权；两种权限档位各失败一次，档位也被排除）。
    #   ⇒ 装了但只接了半条路 —— 而没授权才是**绝大多数时候**的状态。
    # ⛔⛔ 必须走 `本次提醒`（放行时贴），**绝不能 `return 送达警句`** ——
    #   调用方拿到非 None 会当成**拦截**并 deny 掉这次工具调用
    #   ⇒ 那会把「放行」变成「拒绝」，比原来的漏提醒严重得多。
    #   ⭐ **每会话只贴一次**：门铃本就被闸限在 4 次/会话 ⇒ 最多 4 次里贴 1 次。
    #     ⛔ 别每次都贴 —— 本仓有一条明确的反噪音设计「放行 ⇒ 静默」，
    #     第一版每次都贴，当场打挂 4 条老断言。⭐ 那 4 条是真判据，⛔ 不是夹具毛病。
    if not st.get("门铃警句已贴"):
        st["门铃警句已贴"] = True
        _门铃调速器.本次提醒 = 送达警句
    return None


def _撞车卫兵(data, st):
    """PreToolUse：正要写的文件在**最近 30 分钟内被非本会话改过** ⇒ 拦一次要求确认，原样重发即放行。

    ⭐⭐ 机械化的是两条至今**只靠自觉**的纪律（PROTOCOL「动手前查 git status＋mtime」／
    「我读过 ≠ 它没变过」）。实证代价（全是 2026-08-13 当天或在案的）：
      · 一个窗差 2.6 分钟就改了另一窗刚动过的文件，没撞靠的是它自己记得查——⛔ 自觉不是载体；
      · 改动史出了两对重号（## 86、## 93 各两条）——各窗追加前没看对方刚追加了什么；
      · 一封信被追加 3KB 后按旧版归了档。
    ⭐ 设计取舍（⛔ 别改松也别改狠）：
      · 只拦 Write/Edit/NotebookEdit——Bash 写入（重定向/heredoc）拿不准目标路径，**已知盲区**，
        ⛔ 别硬解析命令行去猜（猜错一次误拦的信用代价 > 漏一次）；
      · 新建文件不拦（不存在＝没人的）；30 分钟前的旧改动不拦（不新鲜＝对方多半收工了）；
      · **同一次外部改动只拦一次**（按 mtime 记账）——对方再改一版，才再拦一次；
      · 只在配了信箱的项目生效（信箱＝多窗并行的标记）；子代理内部不拦。
      · 它挡不住 harness 已挡的（Edit 前必须 Read 最新版）——它补的是那之外的：
        「对方可能**还在动**」这个信息，harness 不看，它看。
    """
    if data.get("agent_id"):
        return None
    if str(data.get("tool_name") or "") not in WRITE_TOOLS:
        return None
    cwd = data.get("cwd")
    if _收件目录(cwd) is None:
        return None
    inp = data.get("tool_input")
    inp = inp if isinstance(inp, dict) else {}
    fp = str(inp.get("file_path") or inp.get("notebook_path") or "")
    if not fp:
        return None
    try:
        p = Path(fp)
        if not p.is_file():
            return None
        mt = p.stat().st_mtime
    except Exception:
        return None
    now = time.time()
    if now - mt > 1800:
        return None
    key = fp.replace("\\", "/").lower()
    mine = (st.get("写过") or {}).get(key)
    if mine and mine >= mt - 2:
        return None                      # 最后一笔就是本会话写的
    警过 = st.setdefault("冲突已提示", {})
    if 警过.get(key, 0) >= mt - 1:
        return None                      # 这一次外部改动已经拦过了
    警过[key] = mt
    分 = max(1, int((now - mt) / 60))
    return (
        "⚠️【撞车卫兵】`%s` **%d 分钟前被非本会话改过**——多窗并行下这通常意味着：\n"
        "另一个窗可能**还在动它**，或它已不是你上次看到的版本。30 秒自查：\n"
        "① 重读过它**现在**的内容了吗（尤其你要改的那段附近）？\n"
        "② 台账/清单类：你的编号、条目会不会与对方**刚追加的**撞上（改动史出过两对重号）？\n"
        "③ `git status`／收件箱里有没有对方的在制品说明？\n"
        "⇒ 确认无冲突 ⇒ **原样重发同一调用即放行**（同一次外部改动只拦这一次）。"
        % (fp, 分))


# ---------- 四个入口 ----------

# ---------- 独占资源卫兵（Photoshop 这类「一次只能一个窗用」的外部程序） ----------

def _占用账():
    return STATE_DIR / "占用.json"


def _心跳文件():
    return STATE_DIR / "卡住哨心跳.json"


def _垫片目录(cwd):
    """读项目 `.claude/工作流.yaml` 里的 `垫片目录`。⭐ 让提示里能给出**这个项目真实的**命令。

    ⚠️ ⛔ 别去 import 接入.py 里的同名函数——那是安装器，哨每次工具调用都跑，⛔ 不该拖它进来。
    """
    try:
        f = Path(cwd or os.getcwd()) / 配置目录 / "工作流.yaml"
        if not f.is_file():
            return None
        import yaml
        return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("垫片目录") or None
    except Exception:
        return None


# ⭐⭐ W-5 复核 §五.2 · 方案 A（用户 2026-08-15 拍板）：让「此刻没人盯着」变成看得见的。
#   复核的原问是「谁在什么时刻会真的去开那个终端」，它的答案是：**按原设计，没有人**。
#   ⇒ 那机制要求人在**每次**驱动外部程序**之前主动想起来**开个终端，
#     而这批东西的立仓事故（10.5 小时）恰恰证明**人不会想起来** —— ⭐ 这是个循环。
#   ⇒ 解法：⛔ 不靠人记得，靠**登记那一刻**顺手看一眼心跳。心跳过期 ⇒ 在**放行的同时**贴一句。
心跳过期秒 = 300.0     # ⚠️ 拍的：卡住哨默认每 60 秒写一次 ⇒ 连丢 5 次才判定它没了，⛔ 不误报


def _卡住哨在岗吗():
    """返回 (在岗, 说明)。⛔ 三态：True/False/None（读不出来 ⇒ ⛔ 不许当成"在岗"也不许当"不在"）。"""
    p = _心跳文件()
    try:
        if not p.is_file():
            return False, "从来没有卡住哨在这台机器上跑过（⛔ 没有心跳文件）"
        d = json.loads(p.read_text(encoding="utf-8"))
        t = float(d.get("时刻") or 0)
        if d.get("状态") == "撤防":
            return False, "上一个卡住哨已经**撤防退出**了（%.0f 分钟前）" % ((time.time() - t) / 60.0)
        老 = time.time() - t
        if 老 > 心跳过期秒:
            return False, "最后一次心跳是 **%.0f 分钟前**（⇒ 那个进程多半已经没了）" % (老 / 60.0)
        return True, ""
    except Exception:
        return None, "心跳文件读不出来"


def _读独占资源(cwd):
    """项目在 `.claude/工作流.yaml` 里用 `独占资源:` 声明「哪条命令会去用什么外部工具软件」。

    ⛔⛔ **引擎里故意一条默认正则都不写**，这不是偷懒，是实测结论：
      某图像产线项目驱动 Photoshop 的真实命令形态是 `python tools/slice_psd.py 执行 …`
      ——**字面上一个 "Photoshop" 都没有**（真正的 COM 调用埋在 `tools/psdforge/ps.py` 里）。
      ⇒ 任何"通用"正则（`Photoshop\\.exe` / `\\.jsx` 之类）**都看不见那次 10.5 小时的事故**。
      ⭐⭐ 而一个看不见真实病例的卫兵，**比没有卫兵更糟**——它会让人以为这块有人看着。
      ⇒ 所以：**只认项目自己声明的**，⛔ 不猜。

    ⭐ 未声明 ⇒ 本卫兵**关闭**，且 `接入.py --检查` 会把「本项目没声明独占资源」报出来
      ⇒ ⭐「关着」是**看得见的**，⛔ 不是静静地不干活（同守望「盯到 0 个文件要喊」那条教训）。

    ⭐⭐ **⛔ 这里说的不止 Photoshop**（用户 2026-08-15 点名：「这个规则不止适用于 PS，
      同样适用于其他的工具软件」）。凡是**由脚本驱动、自己带界面**的程序都算同一类：
      Photoshop / Illustrator / Unity / Blender / Excel·COM / 各种安装器与导出器……
      它们共有一个致命形状：**会弹一个模态框在那儿等人点**，而从调用方看，
      「在飞快地干活」和「弹了框干等你」**长得一模一样：都是没返回**。

    配置形状（⭐ `正常时长分钟` 2026-08-15 新增，⛔ 不是可选装饰——见下）：
      独占资源:
        - 名: Photoshop
          命令正则: 'slice_psd\\.py\\s+(执行|切图)|psdforge[\\\\/]ps\\.py|Photoshop\\.Application'
          正常时长分钟: 2        # 这一类调用**正常**跑多久（本项目自己量，⛔ 引擎不猜）

    ⚠️ `正常时长分钟` 没声明 ⇒ 退回引擎兜底 10 分钟，**而那个 10 是拍的**
      （原注释自陈：手上只有"一次正常导出 43 秒、事故那次 10.5 小时"两个点）。
      ⭐ 阈值必须**由项目声明**的道理：一次 PS 导出的正常量级是分钟，一次 Unity 全量构建
      是几十分钟——同一个数不可能同时对。⇒ 裸阈值留在引擎里，就是给每个项目发一个错的基准。
    """
    try:
        f = Path(cwd or os.getcwd()) / 配置目录 / "工作流.yaml"
        if not f.is_file():
            return []
        import yaml
        cfg = (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("独占资源") or []
        out = []
        for it in cfg:
            名, 正则 = (it or {}).get("名"), (it or {}).get("命令正则")
            if 名 and 正则:
                try:
                    分 = (it or {}).get("正常时长分钟")
                    分 = float(分) if 分 is not None and float(分) > 0 else None
                except (TypeError, ValueError):
                    分 = None   # ⛔ 写坏了当没声明（退兜底），⛔ 不许因此整条资源失效
                try:
                    out.append((str(名), re.compile(正则, re.I), 分))
                except re.error:
                    pass      # ⛔ 正则写坏了不许拖垮整个 hook，⛔ 但也别静默——见 --检查 那侧
        return out
    except Exception:
        return []


# ⭐ 没声明 `正常时长分钟` 时的兜底。⚠️ 10 是**拍的**（2026-08-14 起沿用），⛔ 无实测依据
#   ⇒ 项目声明了就用项目的；这个数只是"总比没有强"，⛔ 别当基准引用。
兜底正常分钟 = 10.0

# ⭐⭐ 「一步跨很久」的两条腿——**⛔ 顺序不许换，第一条才是有实测的那条**。
#   2026-08-15 用户亲证撤回了原归因：那 10.5 小时**不是**另一窗抢 PS，是 PS 弹了个
#   「此文档包含嵌套的图层组…」的模态框在等人点，**用户点掉之后进程才继续动**。
#   ⇒ ⛔ 别把"另一窗抢占"删掉（它只是**未被证实**，不是被证伪），但它得排第二。
卡住两因 = ("① ⭐ **它弹了框在等人点**（有实测）——工具软件**⛔ 不止 Photoshop**：任何自带界面的"
            "程序都会弹兼容性提示/覆盖确认/许可证过期/更新提醒。这类框是**首选项级**的，"
            "代码里的「别弹对话框」开关**压不住** ⇒ ⭐ **只有人去点那一下能解开**。\n"
            "② **另一个窗同时在用它**，两边排队（⛔ 未证实，但也没被证伪 ⇒ 仍要防）。")


def _占用卫兵(data, st):
    """PreToolUse：这条命令要去用一个**独占的外部工具软件**，而**别的窗正占着** ⇒ 拦一次。

    ⭐⭐ 立此卫兵的事故（唯一有实测损失的一次）：某项目有一步**跨了 10.5 小时**，
      而且是**用户自己发现窗口不动了**才知道的。
      ⚠️⚠️ **归因已于 2026-08-15 更正**（用户第一手，见 `_dev/收件/已读` 那封红信）：
      主因是**那个程序弹了框在等人点**，⛔ 不是"另一窗抢占"（原说法只有"同期确实有别的窗
      在用它"这个**共存事实**，⛔ 不构成因果；而"点掉框→立刻恢复"是一次**干预**，证据强得多）。
      ⇒ ⛔ 本卫兵**不因此下岗**：抢占那条只是未被证实、⛔ 不是被证伪，它照样是真风险。
      ⇒ 但**排第一该防的是"卡住等人"** ⇒ 那条腿由 `引擎/卡住哨.py` 承（进程外守夜）。

    ⚠️ 现有机制对"抢占"命中数为 **0**：撞车卫兵看的是**文件**，信箱看的是**留言**，
      ⛔ 没有任何一样东西看得见「谁在用那台外部程序」。

    ⭐ 机制的巧处（⛔ 别改成定时清理）：**动手前自动登记、干完自动交还**
      ⇒ 一个**卡住的调用永远不会交还** ⇒ 账上那条"还占着"**正是真相**，⛔ 不是脏数据。
    ⛔⛔ **绝不自动清理陈旧登记**：那次事故本身就是一条**合法持有 10.5 小时**的登记，
      任何"超过 N 分钟就当它结束了"的规则，都会把**恰恰要抓的那一次**抹掉。
      ⇒ 陈旧只**改变措辞**（「可能还在跑，也可能那个窗已经没了——⛔ 这两件分不清」），
        ⛔ 不改变"报出来"这件事。

    ⛔ 只拦一次（同撞车卫兵）：原样重发即放行——**要等还是要抢，是人的决定，⛔ 不是脚本的**。
    """
    _占用卫兵.本次登记 = []
    资源 = _读独占资源(data.get("cwd"))
    if not 资源:
        return None
    文 = _stdin_tool_input_text(data)
    if not 文:
        return None
    我 = str(data.get("session_id") or "无session")
    now = time.time()
    # ⛔⛔ W-5 复核 D3：整个「读账 → 改 → 写回」必须在锁里，⛔ 不许读完就放手。
    #   ⚠️ 拿不到锁**照样往下走**（见 _账锁 纪律 1）——那时退化成老行为（可能掉条），
    #     ⛔ 但绝不许把会话卡住。
    with _账锁() as 锁上了:
        return _占用卫兵内(data, st, 资源, 文, 我, now, 锁上了)


def _占用卫兵内(data, st, 资源, 文, 我, now, 锁上了):
    try:
        账 = json.loads(_占用账().read_text(encoding="utf-8")) if _占用账().is_file() else {}
        if not isinstance(账, dict) or any(not isinstance(v, dict) for v in 账.values()):
            raise ValueError("占用账本不是对象")
    except Exception:
        # W-2 复核修复（回单 §三）：坏账不是空账；拦一次且绝不登记，避免覆盖唯一线索。
        键 = "占用坏账"
        if st.get("冲突已提示", {}).get(键):
            return None
        st.setdefault("冲突已提示", {})[键] = now
        return ("⛔ **【占用账本坏了】** `%s`\n"
                "⛔ 分不清有没有人占着 ⇒ 去看一眼、修好或删掉它再来；"
                "⭐ 原样重发即放行。坏账状态下本次不会登记、不会写盘，⛔ 不覆盖现场。"
                % _占用账().as_posix())
    改了 = False
    for 名, 正则, 正常分 in 资源:
        if not 正则.search(文):
            continue
        持 = 账.get(名)
        # W-2 复核已知窄（回单 §九 e）：窗 resume 换 session id 会被当成别的窗、白吃一次拦截。⛔ 故意不修——分不清「还是我」与「另一个窗」时，宁可多拦一次（拦错的代价是原样重发，放错的代价是 10.5 小时）。
        if 持 and 持.get("会话") and 持["会话"] != 我:
            分 = (now - float(持.get("时间") or now)) / 60.0
            键 = "占用:" + 名
            if st.get("冲突已提示", {}).get(键):
                continue          # ⭐ 拦过一次就放行（人已经知道了，⛔ 别反复拦）
            st.setdefault("冲突已提示", {})[键] = now
            久 = ("⚠️ 登记于 **%.0f 分钟前且从未交还** —— 可能**还在跑**，"
                  "也可能**那个窗已经没了**。⛔ 这两件从账上分不清，去看一眼。" % 分
                  if 分 >= 120 else "已占用 **%.0f 分钟**。" % 分)
            return ("⛔ **【%s 被别的窗占着】** %s\n"
                    "  · 占用方会话：`%s`\n"
                    "  · 它当时在跑：`%s`\n\n"
                    "⭐ **为什么拦你**：唯一有实测损失的事故就出在这一格——有一步**跨了 10.5 小时**，"
                    "还是用户发现窗口不动了才知道的。⛔ 不能再静默排队。\n"
                    "⚠️ **那次的主因后来查明是「它弹了框在等人点」，⛔ 不是抢占**——但抢占这条"
                    "只是未被证实、⛔ 没被证伪 ⇒ 两条都得防，而它们**从外面看长得一模一样：都是没返回**。\n"
                    "⇒ **四选一，⛔ 别默认往下冲**：① ⭐ **先去看一眼那个程序的界面上有没有框等着点**"
                    "（这是唯一只有人能解开的那种卡法）② 等它（去问那个窗还要多久）"
                    "③ 换个不碰 %s 的活先做 ④ 确认那个窗其实已经死了 ⇒ 删掉 `%s` 里那条登记。\n"
                    "⭐ 想清楚了**原样重发即放行**（本会话⛔ 不再拦这个资源）。"
                    % (名, 久, 持["会话"], str(持.get("干什么") or "")[:90], 名,
                       _占用账().as_posix()))
        # 没人占（或就是我）⇒ 登记上，⛔ 别只查不记（只查不记＝下一个窗照样看不见我）
        # ⭐ `正常分钟` 一并写进账：⛔ 让进程外的 `卡住哨.py` 不必去读每个项目的 yaml——
        #   它只认这一本账，就能对**任何项目、任何工具软件**判「这次是不是卡太久了」。
        账[名] = {"会话": 我, "时间": now, "干什么": 文[:200],
                  "工具": str(data.get("tool_name") or ""),
                  "正常分钟": 正常分 if 正常分 else 兜底正常分钟,
                  "阈值来源": "项目声明" if 正常分 else "引擎兜底(拍的)",
                  "项目": Path(data.get("cwd") or os.getcwd()).name,
                  # ⭐ W-5 复核 D10：子代理也登记了 ⇒ 记下是谁，⛔ 免得回头分不清
                  "子代理": str(data.get("agent_id") or ""),
                  # ⭐⭐ 2026-08-16：把「这一条是在没锁的情况下写的」**记进条目本身**。
                  #   ⚠️ 病灶：抢不到锁时按纪律**照常往下走**（fail-open 优先，⛔ 不许卡住会话），
                  #     ⛔ 但那时可能掉条，而**掉条是静默的** —— 账读得出、格式正常、
                  #     卡住哨照印「✅ 在岗 · N 条登记」，⛔ 没有任何一侧会发现少了几条。
                  #   ⭐ 复核 D3 的原话：**掉条比乱喊糟，就糟在它静默**。
                  #   ⇒ 修法⛔ 不是加长锁超时（哨挂在每次工具调用上，等待会拖慢会话），
                  #     而是**让这次"没锁上"留下痕迹** ⇒ 事后看得出这条记录不可靠。
                  "无锁写入": (not 锁上了)}
        _占用卫兵.本次登记.append(名)      # ⭐ 供「卡住哨在不在岗」那句提示用
        改了 = True
    if 改了 and not 锁上了:
        # ⭐ 除了记进条目，还进一次触发日志 ⇒ **事后盘得出「无锁写入」到底多常见**
        #   （⛔ 不出声打扰用户：这一次多半没掉条，吵他属于误报；
        #     但**完全不留痕**就又变成"沉默有两种含义"了。）
        try:
            _log_trigger("占用账本·无锁写入", "PreToolUse",
                         "抢锁超时仍写入：%s（并发时可能掉条）" % "、".join(_占用卫兵.本次登记),
                         str(data.get("session_id") or ""))
        except Exception:
            pass
    if 改了:
        _写占用账(账)
    return None


import contextlib


@contextlib.contextmanager
def _账锁(超时=2.0, 账=None, 陈旧秒=30.0):
    """账本的读-改-写互斥锁。⛔⛔ W-5 复核 D3：⛔ 别去掉。

    ⭐ `账` 缺省是占用账本；门铃授权那本传自己的路径。
      ⚠️ 2026-08-16 工作流开发窗来信要求「复用这把锁，⛔ 别再造一把」——
      ⭐ 本窗的读法：⛔ 别再写第二份**锁实现**（那才会两份逻辑漂移）；
        但**⛔ 不该让两本无关的账共用同一个锁文件**——那会让门铃回写去跟占用登记排队，
        平白多一次等待，而哨挂在每次工具调用上。⇒ 同一份实现、各锁各的。

    ⚠️ 实测（复核 `e7_conc4.py`）：4 个窗同时登记 × 15 轮 ⇒ **10 轮掉条、2 轮把账写坏**。
      而**掉条是静默的**——账读得出、格式正常、卡住哨照印「✅ 在岗 · N 条登记」，
      ⛔ 没有任何一侧会发现少了几条 ⇒ 那几个窗卡住时**永远不会有人喊**。
      ⭐ 复核的判断（我认同）：**掉条比写坏糟得多**——乱喊你会去看，漏喊你永远不知道要去看。
    ⚠️⚠️ 两条硬纪律（⛔ 别删）：
      1. **拿不到锁也必须往下走**（超时即放弃加锁）——哨挂在每次工具调用上，
         ⛔ 绝不许因为抢不到锁把用户的会话卡住。fail-open 优先于一致性。
      2. **抢占陈旧锁**：锁文件比 `陈旧秒` 还老 ⇒ 多半是持有者进程死了，删掉再抢。
         ⛔ 不然一次崩溃会让这本账**永久锁死**——那又是一个静默失效。

    ⛔⛔ **陈旧阈值必须显著大于抢锁超时**（2026-08-16 被回归测试当场逼出来的真缺陷）：
      ⚠️ 原来两者是**同一个数**（都用 `超时`）⇒ **持有者只要持有超过 2 秒，
        锁就会被别人当"陈旧"删掉抢走** ⇒ 两个进程同时读-改-写 ⇒ **锁形同虚设**。
      ⭐ 而"正常持有"只是一次读＋一次写（毫秒级）⇒ 30 秒之外还占着的，基本只能是**死掉的持有者**。
      ⚠️ 30 是**拍的**，⛔ 无实测依据——取值思路只有一条：**比正常持有量级高三四个数量级**。
      ⇒ ⭐ 这个缺陷是怎么现形的值得记：测试里手动占住锁、期望"抢不到"，
        结果**抢到了** —— 因为哨从启动到这一行花了 >2 秒，锁被自己判成陈旧。
        ⛔ 若不是那条断言红了，这把锁会一直"看着在、其实常被绕过"。
    """
    锁 = (Path(账) if 账 else _占用账()).with_suffix(".lock")
    fd = None
    截止 = time.time() + 超时
    while time.time() < 截止:
        try:
            fd = os.open(str(锁), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(str(锁)) > 陈旧秒:
                    os.unlink(str(锁))      # ⭐ 陈旧锁：持有者多半死了，⛔ 别让它锁死这本账
                    continue
            except OSError:
                pass
            time.sleep(0.02)
        except OSError:
            break                            # 目录不可写之类 ⇒ ⛔ 不加锁也要往下走
    try:
        yield fd is not None                 # ⭐ 把「有没有真的锁上」交出去，⛔ 不假装成功
    finally:
        if fd is not None:
            try:
                os.close(fd)
                os.unlink(str(锁))
            except OSError:
                pass


def _写占用账(账):
    """原子写占用账本。⛔⛔ W-5 复核 D2：⛔ 别改回 `write_text` 直写。

    ⚠️ 老写法（先清空再写）中途失败一次 ⇒ 留下一个 **0 字节**的账本 ⇒ 此后**每个窗**都被拦一次
      「账本坏了」，重发放行 ⇒ **再也没有任何登记写得进去，也没有任何报错**（复核实跑复现）。
    ⭐ 本函数与 `_state_save` 用同一套办法（临时文件 ＋ `os.replace` 原子替换）——
      那边 2026-08-13 就因为同一个病改过了，⛔ 这两处是当时漏掉的（全库仅有的两处直写）。
    ⚠️ 顺手修掉一处**纯空操作**：原来这里写着 `_可写(_占用账().parent)`，看着像"确保目录可写"，
      其实 `_可写()` 是**洗字符串**的函数，传 Path 进去内部抛异常被吞掉、返回空串
      ⇒ 既不建目录也不检查任何东西。⇒ 换成真的 `mkdir`。
    """
    try:
        p = _占用账()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        # ⭐ _可写() 在这里是**对的**用法：洗掉孤立代理项，⛔ 免得 json 写到一半抛异常
        tmp.write_text(_可写(json.dumps(账, ensure_ascii=False, indent=1)), encoding="utf-8")
        os.replace(str(tmp), str(p))
    except Exception:
        _log_error()      # ⛔ 别静默：写不进去 ＝ 后面所有登记都白登，至少要在错误日志里留痕


def _占用交还(data):
    """PostToolUse：这条命令跑完了 ⇒ 把我登记的那个资源交还，并**报出耗时**。

    ⭐ 耗时是白捡的：登记在动手前、交还在干完，两个时间一减就是真实耗时
      ⇒ 顺带实现了执行窗提的那个"穷办法"（**调用耗时异常就出声**），⛔ 不用另造机制。

    ⭐ 阈值 2026-08-15 改为**项目声明**（`独占资源:` 的 `正常时长分钟`）——原来写死的 10
      是拍的，而"正常多久"本就因工具而异（PS 导出是分钟级，Unity 全量构建是几十分钟级）。
      ⭐ **旧值原地留存**为 `兜底正常分钟 = 10.0`：没声明的项目行为**一字不变**，
      且报警里**明说这个数是哪来的**（⛔ 不许让人把拍的数当基准）。

    ⚠️⚠️ **本函数的根本局限（⛔ 别指望它救那 10.5 小时）**：它是 PostToolUse ——
      **调用返回之后**才跑。卡住的那 10.5 小时里它一次也没机会开口，
      事后报一句「这步用了 630 分钟」是**马后炮**。
      ⇒ ⭐ 当场喊人的那一半在 `引擎/卡住哨.py`（进程外守夜，⛔ 不受本窗阻塞影响）。
    """
    资源 = _读独占资源(data.get("cwd"))
    if not 资源 or not _占用账().is_file():
        return None
    # ⛔ W-5 复核 D3 配套：交还也是「读-改-写」⇒ 同样必须在锁里，⛔ 否则照样互相覆盖
    with _账锁():
        return _占用交还内(data, 资源)


def _占用交还内(data, 资源):
    try:
        我 = str(data.get("session_id") or "无session")
        try:
            账 = json.loads(_占用账().read_text(encoding="utf-8"))
            if not isinstance(账, dict) or any(not isinstance(v, dict) for v in 账.values()):
                raise ValueError("占用账本不是对象")
        except Exception:
            # W-2 复核修复（回单 §三）：交还时读到坏账就静默离开，⛔ 绝不把坏文件重写掉。
            return None
        文 = _stdin_tool_input_text(data)
        说 = []
        改了 = False
        for 名, 正则, 正常分 in 资源:
            持 = 账.get(名)
            if not 持 or 持.get("会话") != 我:
                continue
            # ⛔⛔ W-5 复核 D9：原来是 `if 文 and not 正则.search(文)` ——
            #   `文` 取不到时**跳过整个正则判断直接往下走**，落进 `del 账[名]`
            #   ⇒ 一次 `tool_input` 为空的调用能把**还占着的登记直接交还掉**。
            #   ⚠️ 且子代理与主窗共用 session_id（见 _查收件 那侧注释）⇒ 子代理的一次空调用
            #     能抹掉主窗的登记 ⇒ 卡住哨从此盯的是空的。
            #   ⇒ ⭐ 取不到文本就**别交还**：多喊一声的代价，远小于漏喊一次。
            if not 文 or not 正则.search(文):
                continue
            用了 = (time.time() - float(持.get("时间") or time.time())) / 60.0
            门 = 正常分 or 兜底正常分钟
            # ⭐⭐ W-5 复核 U2（判不了那一档）的修法，用户 2026-08-15 拍板做：
            #   **每一次**的耗时都记下来，⛔ 不只记超时那几次。
            #   ⚠️ 病根：原来低于门槛的 `用了` 算出来就扔了 ⇒ 触发日志里只有超时的那几条
            #     ⇒ **谁都算不出误报率**（连"一共跑过几次"都不知道）。
            #   ⭐ 复核原话：「那个数据本来就在手上，只是被扔了」。⇒ 几行代码换回一条基线。
            #   ⇒ 攒够十几次后可以算：① 真实的正常上界（用来校 `正常时长分钟`）
            #     ② 超门槛次数 ÷ 总次数 ＝ **真实误报率**（⛔ 不再靠从阈值推）。
            try:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                with (STATE_DIR / "外部程序耗时.jsonl").open("a", encoding="utf-8") as _f:
                    _f.write(json.dumps({
                        "t": time.strftime("%Y-%m-%d %H:%M:%S"), "资源": 名,
                        "项目": str(持.get("项目") or ""), "用了分钟": round(用了, 3),
                        "门槛分钟": 门, "超门槛": bool(用了 >= 门),
                        "阈值来源": str(持.get("阈值来源") or ""),
                        "子代理": bool(持.get("子代理")),
                    }, ensure_ascii=False) + "\n")
            except Exception:
                pass      # ⛔ 记账失败不许影响交还本身——账清不掉才是真损失
            来源 = ("本项目声明的正常时长 %g 分钟" % 门 if 正常分
                    else "引擎兜底 %g 分钟（⚠️ 这个数是**拍的** ⇒ 本项目该在 "
                         "`.claude/工作流.yaml` 的 `独占资源:` 里声明 `正常时长分钟`）" % 门)
            del 账[名]
            改了 = True
            if 用了 >= 门:
                # ⭐ 不足 1 分钟说秒（同 卡住哨）：「用了 0 分钟」读起来像坏了，⛔ 会让人不信这条提醒
                用了文 = ("%.0f 秒" % (用了 * 60)) if 用了 < 1 else ("%.0f 分钟" % 用了)
                说.append("⏱️ **%s 这一步用了 %s**（判据：%s）\n"
                          "⇒ ⭐ 要么它真的就是这么久，要么**这中间有一大段是在干等**。⛔ 别默认前者。\n"
                          "**两种常见卡法，⛔ 顺序不许换**：\n%s\n"
                          "⇒ ⭐ **下次开跑前**在旁边守一个：`python <垫片目录>/卡住哨.py`"
                          "——它在进程外数时间，超时会**当场喊人去点那一下**，"
                          "⛔ 不像这条提醒是事后才说得出口的。"
                          % (名, 用了文, 来源, 卡住两因))
        if 改了:
            _写占用账(账)      # ⛔ 同 D2：原子写，⛔ 别改回直写
        return "\n".join(说) or None
    except Exception:
        return None


# ---------- 许可尾巴（用户 2026-08-14 拍板：条目是逼多想一层，⛔ 不是逼转向） ----------

# ---------- 常驻块重放（用户 2026-08-17 拍板） ----------

# ⭐ 每长这么多字节的转录，重放一次自问清单。⚠️ 1 MB ≈ 300k token 是**粗估的代理换算**：
#   实测本机转录 4.8 MB 的会话≈150 万 token 量级 ⇒ 约 3.2 MB/百万 token ⇒ 300k ≈ 0.95 MB。
#   ⛔ 它随内容类型漂移（附件多就虚高）⇒ 只用来判「大概长了多少」，⛔ 不是精确计量。
#   ⚠️ 300k 这个数是**用户给的**，唯一依据是「C-18 跨过 1M 后出过问题」这一个点 ⇒ 用起来后回校。
重放字节 = int(os.environ.get("WORLDBOOK_重放字节") or 1024 * 1024)

# ⭐⭐ 重放的**不是原文，是「该问自己什么」**（用户 2026-08-17 拍板的形态）。
#   道理：模型走偏⛔ 不是因为忘了内容——内容它开窗时读过；
#     是**那个念头没在该起的时候起来**。⇒ 重放要唤起念头，⛔ 不是重传信息。
#   ⚠️ 「短提要比全文更有效」目前是**推断，⛔ 无实测** ——已当面向用户交代。
#
# ⛔ 为什么只有两条（⛔ 别顺手把五条都塞回来）：
#   · JF-102 较真不是施压 → 2026-08-17 用户拍板**改成触发式**（UserPromptSubmit：
#     用户否定我的那一刻才响）⇒ 它有明确时刻 ⇒ ⛔ 不必占「每轮在场」的位置。
#   · JF-103 候选蒸馏 → 已搬去 Stop（收工才用得上，⛔ 不必每轮在场）
#   · JF-104 回报语言 → **格式有惯性**（前文就是参照），且它失效时**用户当场看得出**
#     （报告变难懂）⇒ ⭐ 有用户兜底 ⇒ ⛔ 不占重放的位置
#   · 留下的这两条，共同点是**失效时用户察觉不到**——那才是必须靠机制兜的。
#
# ⭐⭐ 末尾那句「⛔ 不是在说你刚做错了」是**用户点名要的**（2026-08-17 原话）：
#   「这类真常驻在重新注入时应补充类似语义段（**避免模型以为当下做错了而矫枉过正**）：
#     此为再次注入强调模型行为，**非当场矫正模型行为**。」
#   ⚠️ ⛔ 别把它删成「太啰嗦」——它治的是一个真实副作用：一段突然出现的纪律提醒，
#     模型最容易读成「我刚犯了这个错」，然后去推翻一个本来正确的判断。
重放清单 = ("🔁 **对话已经很长了，把这两句在心里过一遍**（常驻块多半已被前面的内容淹没）：\n"
            "  ① 这一轮我有没有**更好的做法没摆出来**？——用户措辞笃定⛔ 不等于方案已定死。\n"
            "  ② 我要说「做完了」吗？**有一次真东西从头流到尾吗**？它被用上了吗、起作用了吗？\n"
            "⇒ 两句都过得去就继续。全文在 `{纠偏库}/active/JF-10*.yaml`，要核就去读。\n"
            "\n"
            "⚠️ **这是定时重申，⛔ 不是在说你刚做错了什么。**\n"
            "⇒ 它按对话长度自动出现（与你这一步做得对不对**无关**）"
            "⇒ ⛔ 别因为看到它就去推翻一个本来正确的判断。")


def _该重放吗(data, st):
    """转录文件比上次重放时长了 ≥ 重放字节 ⇒ 该重放。返回 (该不该, 现在多大)。

    ⭐⭐ 立此机制的实证（用户 2026-08-17 亲历）：常驻块**只在开窗注入一次**
      ⇒ 会话一长就被对话淹没；上下文被压缩时**可能整个消失**。
      而它消失和它在场**长得一模一样**——模型⛔ 不会说「我忘了那条纪律」，
      它只是**行为悄悄退回默认**。用户实测撞过一次（跨过百万上下文、压缩过的窗明显走偏，零报错）。

    ⚠️ 为什么拿**转录文件大小**当尺子：hook 的 stdin 里**没有任何 token 计数字段**
      （实测 202 个样本，字段全在 `_state/实测采样.jsonl`）——
      但 `transcript_path` **201/202 次都有** ⇒ 文件就在盘上，单调增长，stat 一次的代价可忽略。
      ⛔ 它是**代理指标**，⛔ 不是真 token 数。
    """
    try:
        p = data.get("transcript_path")
        if not p:
            return False, 0
        大小 = os.path.getsize(p)
        上次 = int(st.get("上次重放字节") or 0)
        if 上次 == 0:
            # ⭐ 第一次见到这个会话：**记下基线就走**，⛔ 别立刻重放
            #   （开窗时刚注入过全量，紧接着再来一遍纯属噪音）
            return False, 大小
        return (大小 - 上次) >= 重放字节, 大小
    except Exception:
        return False, 0


许可尾巴 = ("\n——\n⭐ 本提醒是让你**多想一层**，⛔ 不是让你必须转向：把另一条路真核一遍，"
            "核后**维持原判也是正确结局之一**——但要**用一句话说出为什么**再继续/原样重发。"
            "⛔ 一个字不说的原样重发＝没理会；「查过并维持原路」必须和「没理会」长得不一样。")


def _带许可(e):
    """纠偏条目的注入文本统一带上「可以不改」的许可（引擎级一处加，⛔ 不逐条改 23 份文本）。

    ⭐ 为什么（用户 2026-08-14 拍板，W-2 复核窗论证）：
      ① 注入文本口气笃定，误触发时会把拿不准的模型推向「顺从改掉」——而 W-2 实证过
        误触发咬中「正在做对的事」的样本（JF-015 唯一一次正则命中）⇒ 顺从＝把对的改错。
      ② 「机制不靠自觉」对模型同样成立：⛔ 别拿"对齐会抵抗盲从"当保险——那是概率不是机制，
        同一批条目还喂 codex 等其他执行体。
      ③ 要求「说一句理由再维持原路」⇒ 让「想过没改」在转录里可见
        ⇒ 减速带审计从此分得出「想过没改」和「无视」（W-2 审计原本分不出这两种）。
    ⛔ 范围：只加给纠偏条目的四个注入出口。常驻（SessionStart）不是纠偏、不加；
      撞车/占用两个卫兵有自己的选项文案、守的是有实测损失的事（10.5 小时排队），⛔ 不软化。
    """
    return _填路径(str(e["注入文本"]).strip()) + 许可尾巴


def do_pre_tool_use(data):
    """⭐⭐ 工具**跑之前**拦一次要求自查（自查后原样重发即放行，⛔ 不硬拦）。

    立此入口的事故（2026-08-12）：协作纪律「体力活派给外部执行器、主窗只写 prompt/
    拆单/验收/终裁」写在项目文档里、基建也全好，但**没有任何东西会在「我正要起子代理」那一刻
    把它送到眼前** ⇒ 主窗自己起了 6 个顶配子代理烧掉 63 万 token。PostToolUse 来不及（活已经派出去了），
    Stop 太晚（钱已经花完了）⇒ ⭐ 只有 PreToolUse 站在决策当口。

    ⛔ 两条安全线（别删）：
      1. **必须有 `工具正则`** —— 缺了就跳过。否则一条条目会 deny 掉**每一个**工具调用 ＝ 会话废掉。
      2. **带 agent_id 的调用不拦** —— 那是子代理内部的动作，拦它只会在主窗看不见的地方反复卡住。
    """
    sid = data.get("session_id")
    cwd = data.get("cwd")
    if data.get("agent_id"):
        # ⛔⛔ W-5 复核 D10：原来这里**直接 return** ⇒ 子代理去驱动外部程序时**完全不上账**
        #   ⇒ ⭐ **越守「体力活派给子代理」这条纪律的项目，卡住哨盯的越是空的**。
        #   ⚠️ 但「不拦」是有理由的安全线（拦子代理只会在主窗看不见的地方反复卡住）
        #   ⇒ ⭐ **「不拦」和「不记账」是两件事**：登记是记账，⛔ 不是拦截。
        #     ⇒ 照样登记（并且**丢掉**它可能返回的拦截文案），然后照样返回。
        try:
            _占用卫兵(data, _state_load(sid))
        except Exception:
            _log_error()
        return
    st = _state_load(sid)
    _accumulate(data, st, 只记派单=True)   # ⭐ 派单在**发出那一刻**就记，⛔ 不等它跑完
    # ⭐ 结构卫兵先于条目：撞车与门铃的损失都在**动作发生的瞬间**就成立，事后提醒来不及
    # ⭐ 占用卫兵排在最后：前两个看的是**本机文件**（便宜），它要读项目 yaml（稍贵），
    #   ⛔ 但顺序⛔ 不影响正确性——三者互斥，一次只可能有一个成立。
    冲 = _门铃调速器(data, st) or _撞车卫兵(data, st) or _占用卫兵(data, st)
    if 冲:
        _log_trigger("门铃调速器" if "门铃" in 冲[:20]
                     else "占用卫兵" if ("被别的窗占着" in 冲[:40] or "占用账本坏了" in 冲[:40]) else "撞车卫兵",
                     "PreToolUse", 冲.splitlines()[0][:160], sid)
        _state_save(sid, st)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": 冲}}, ensure_ascii=False))
        return
    # ⭐⭐ W-5 复核 §五.2 方案 A：刚登记了一个独占资源 ⇒ 顺手看一眼「此刻有没有人在盯」。
    #   ⛔ 这句**不拦**（走 additionalContext，工具照跑）——它只是让「关着」看得见。
    #   ⭐ 时机是**正要动手那一刻**，⛔ 不是事后：原来那句「下次开跑前守一个」只在超时后才印，
    #     等于**你先被烧一次，它才告诉你下次记得**。
    # ⭐ 门铃被授权放行时贴的那句（含实时用量）。走 additionalContext ⇒ **⛔ 不拦，工具照跑**。
    #   ⚠️ 它必须真的到模型眼前 —— 只记账不出声的话，「五道闸此刻全关」⛔ 没人知道，
    #     而那正是最该让人看见的状态（沉默不许有两种含义）。
    #   ⭐ 用函数属性传，⛔ 不塞进 st —— st 会落盘，这句只活这一次调用。
    哨提示 = getattr(_门铃调速器, "本次提醒", None)
    _门铃调速器.本次提醒 = None
    if getattr(_占用卫兵, "本次登记", None):
        在岗, 说明 = _卡住哨在岗吗()
        if 在岗 is not True:
            _垫 = _垫片目录(cwd)
            哨提示 = ("⚠️ **你正要用「%s」，但此刻没有卡住哨在岗**（%s）。\n"
                      "⇒ 意味着：这一步如果**卡住等人点框**，⛔ 没有任何人会喊你——"
                      "本项目出过一次这样的事，**干等了 10.5 小时**，还是用户自己发现窗口不动才知道的。\n"
                      "⇒ ⭐ 另开一个终端跑：`python %s/卡住哨.py`（它在进程外数时间，超时会喊人）。\n"
                      "⛔ 本次调用照常放行，⛔ 不拦你。"
                      % ("、".join(_占用卫兵.本次登记), 说明, _垫 or "<垫片目录>"))
            _log_trigger("卡住哨不在岗", "PreToolUse", 说明[:120], sid)
    entries = _load_entries("PreToolUse")
    if not entries:
        if 哨提示:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "additionalContext": 哨提示}}, ensure_ascii=False))
        _state_save(sid, st)
        return
    tool_name = str(data.get("tool_name") or "")
    _inp = data.get("tool_input")
    _fp = str((_inp or {}).get("file_path") or (_inp or {}).get("notebook_path") or "") \
        if isinstance(_inp, dict) else ""
    ctx = {"有派单配置": _has_dispatch_cfg(cwd), "派过单": bool(st.get("派过单")),
           # ⭐ 没有 file_path 的调用（非写入类工具）判「不是代码文件」——
           #   这样「非代码文件」卫兵对它们是放行的，⛔ 不会把无关工具一并噤声
            "是代码文件": bool(_fp and CODE_EXT_RE.search(_fp))}
    inject = []
    # W-2 复核修复（回单 §五 a）：本入口只攒增量，结束时合并写一次仪表账。
    评估增量 = {}
    for e in entries:
        eid = str(e["id"])
        if st["counts"].get(eid, 0) >= int(e.get("冷却", 1)):
            continue
        tool_re = e.get("工具正则")
        if not tool_re:
            continue          # ⛔ 安全线 1：没写工具正则的 PreToolUse 条目一律不生效
        _记评估(评估增量, eid, "评估")
        if not re.search(str(tool_re), tool_name):
            continue
        if not _scope_ok(e, cwd):
            continue
        命中 = tool_name
        rx = e.get("触发正则")
        if rx:                # 可选：再按工具输入内容过一道
            text = _stdin_tool_input_text(data)
            if not text or _is_self_content(text):
                continue
            m = re.search(str(rx), text)
            if not m:
                continue
            命中 = text[max(0, m.start() - 60):m.end() + 60]
        if not _cond_ok(e, ctx):
            _记评估(评估增量, eid, "条件挡")
            continue
        # ⭐ 自建目录豁免（用户 2026-08-19 拍板）：删自己当轮建的 ⇒ 放行＋一行提示，⛔ 不硬拦。
        #   ⛔ 不占本条的冷却次数——放过自建的之后，真删别人的目录**照样硬拦**。
        if e.get("自建目录不硬拦") and tool_name in SHELL_TOOLS and \
           _像删自建目录(_stdin_tool_input_text(data), st):
            _记评估(评估增量, eid, "条件挡")
            _log_trigger(str(eid) + "·自建放行", "PreToolUse", str(命中)[:120], sid)
            inject.append({"注入文本":
                "⭐ 这个递归删除的目标看着是**本会话自己建的**"
                "（同一条命令先建后删，或先前命令建过同一路径）⇒ 放行不拦。\n"
                "⚠️ 若删的其实不是你建的 ⇒ 停下再核一次。"})
            continue
        _记评估(评估增量, eid, "命中")
        先前 = st["counts"].get(eid, 0)
        st["counts"][eid] = 先前 + 1
        _log_trigger(eid, "PreToolUse", "%s ← %s" % (tool_name, 命中), sid)
        处置 = str(e.get("处置", "拦一次"))
        # ⭐⭐ 「拦一次后提醒」阶梯（用户 2026-08-19 拍板：硬拦别超一次，其余用提醒）：
        #   第 1 次硬拦 → 第 2 次**静默放行**（＝「判定必须起就发第二次」的出口；
        #   ⛔ 别删这一格：模型刚读完整段拦截文本，紧接着的重发再提醒一遍＝纯噪音）
        #   → 第 3、4 次**只提醒不拦**（用 提醒文本，缺了退回全文）→ 之后静默。
        #   ⚠️ 用这个处置的条目 冷却 必须设 4 —— 顶上的冷却闸会在 counts≥冷却 时剪断阶梯。
        if 处置 == "拦一次" or (处置 == "拦一次后提醒" and 先前 == 0):
            _合并评估计数(评估增量)
            _state_save(sid, st)
            # ⭐ 拦截时把「没人盯着」那句一并带上：被拦的这一刻人正好在看，⛔ 别浪费这次注意力
            理由 = _带许可(e) + (("\n\n" + 哨提示) if 哨提示 else "")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": 理由}}, ensure_ascii=False))
            return            # 一次只拦一条
        if 处置 == "拦一次后提醒":
            if 先前 == 1:
                continue      # 静默放行那一格（出口，⛔ 不是漏）
            e = dict(e, 注入文本=str(e.get("提醒文本") or e.get("注入文本")))
        inject.append(e)
    段前 = ([哨提示] if 哨提示 else []) + [_带许可(e) for e in inject]
    if 段前:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n\n".join(段前)},
        }, ensure_ascii=False))
    _合并评估计数(评估增量)
    _state_save(sid, st)


def do_post_tool_use(data):
    sid = data.get("session_id")
    cwd = data.get("cwd")
    st = _state_load(sid)
    _accumulate(data, st)          # ⭐ 先累计再判条目：⛔ 与命不命中无关，阈值靠它才有历史可看
    # ⭐ 与条目库无关 ⇒ 放在循环之前，⛔ 别让条目为空时饿死它
    # ⭐⭐ 它现在**会返回一段话**（认领即宣告，见该函数 docstring 里那个死锁）⇒ ⛔ 别丢掉返回值
    宣告 = _认领门铃授权(data)
    entries = _load_entries("PostToolUse")
    # ⛔ 条目为空也不许早退——**收件箱提醒不依赖条目库**。
    #   实测（2026-08-13 回归当场抓的）：早退把后面的信箱检查一并饿死，
    #   条目库空/加载失败的项目会变成「信箱看着挂了、其实根本没轮到它跑」。
    tool_name = str(data.get("tool_name") or "")
    out_text = _stdin_tool_response_text(data)
    ctx = {"有派单配置": _has_dispatch_cfg(cwd), "派过单": bool(st.get("派过单"))}
    records = None
    inject = []
    # W-2 复核修复（回单 §五 a）：同一 hook 的所有条目统计只在末尾合并落一次盘。
    评估增量 = {}
    # ⭐ 独占资源交还：这一步跑完了 ⇒ 把登记撤掉，顺带报出异常耗时。
    #   ⛔ 放在条目循环**之前**——它与条目命不命中无关，⛔ 别让条目库为空时把它一起饿死
    #   （同上面那条实测教训：早退曾把收件箱检查一并饿死）。
    # ⛔⛔ 2026-08-15 端到端实跑抓出的真 bug（⛔ 别再合并这两个容器）：
    #   原代码是 `inject.append(还)` —— 而 `还` 是**字符串**，`inject` 里装的是**条目 dict**。
    #   下游 `_带许可(e)` 会去取 `e["注入文本"]` ⇒ 字符串下标当场 TypeError
    #   ⇒ 被 fail-open 吞掉 ⇒ **整个 PostToolUse 的输出全部消失**：
    #     耗时报警没了、**收件箱提醒没了、本次命中的每一条纠偏注入也全没了**。
    #   ⭐⭐ 它藏得住的原因，正是本库反复在治的那个形状：
    #     `还` 只在**超时**时才非空 ⇒ 平时永远是 None ⇒ 这条路**只在事故那一刻才被走到**，
    #     而那一刻它自己也哑了。⇒ **卫兵恰恰在它存在的理由发生时失灵**。
    #   ⚠️ 它逃过了 W-2 独立复核和 36 组回归：两边测占用都在「没超时」的场景里。
    耗时报 = _占用交还(data)
    if 耗时报:
        _log_trigger("占用卫兵·耗时", "PostToolUse", 耗时报.splitlines()[0][:160], sid)
    for e in entries:
        eid = str(e["id"])
        cool = int(e.get("冷却", 2))
        if st["counts"].get(eid, 0) >= cool:
            continue
        if not _scope_ok(e, cwd):
            continue
        tool_re = e.get("工具正则")
        if tool_re and tool_name and not re.search(tool_re, tool_name):
            continue
        _记评估(评估增量, eid, "评估")
        target = e.get("目标", "工具输出")
        hit_snippet = None
        if target == "工具输出":
            text = out_text
            if not text:
                if records is None:
                    records = _tail_records(data.get("transcript_path"))
                rs = _recent_tool_results(records, 1)
                text = rs[-1]["text"] if rs else ""
            if text and not _is_self_content(text):
                m = re.search(str(e.get("触发正则") or ""), text)
                if m:
                    hit_snippet = text[max(0, m.start() - 60):m.end() + 60]
        elif target == "工具输入":
            text = _stdin_tool_input_text(data)
            if not text:
                if records is None:
                    records = _tail_records(data.get("transcript_path"))
                tus = _tool_uses(records[-6:])
                text = json.dumps(tus[-1][1], ensure_ascii=False) if tus else ""
            if text and not _is_self_content(text):
                m = re.search(str(e.get("触发正则") or ""), text)
                if m:
                    hit_snippet = text[max(0, m.start() - 60):m.end() + 60]
        elif target == "连败序列":
            if records is None:
                records = _tail_records(data.get("transcript_path"))
            rs = _recent_tool_results(records, 12)
            k = _consecutive_failures(rs)
            if k >= int(e.get("连败阈值", 3)):
                hit_snippet = "连败 %d 次；末条：%s" % (k, rs[-1]["text"][:120] if rs else "")
        elif target == "累计代码写入":
            # ⭐ 慢病没有单点信号（一次编辑永远正常）⇒ 只有会话累计量看得见「主窗下场干起了体力活」
            n = len(st.get("代码文件") or [])
            if n >= int(e.get("累计阈值", 4)):
                hit_snippet = "本会话主窗自己写了 %d 个代码文件：%s" % (
                    n, "、".join(Path(x).name for x in st["代码文件"][:6]))
        if hit_snippet is None:
            continue
        if not _cond_ok(e, ctx):
            _记评估(评估增量, eid, "条件挡")
            continue
        _记评估(评估增量, eid, "命中")
        inject.append(e)
        st["counts"][eid] = st["counts"].get(eid, 0) + 1
        _log_trigger(eid, "PostToolUse", hit_snippet, sid)
    # ⭐ 收件箱：每次工具调用后顺手看一眼（这是**拉**，⛔ 不是推——外部推不进一个闲置的窗）。
    #   对正在干活的窗，延迟 ≈ 一次工具调用；对闲着的窗，它本来也没在干活，一恢复就看见。
    段 = []
    # ⭐⭐ 认领宣告排**最前**：它是「你从这一刻起是总管」——⛔ 比任何提醒都该先看到
    if 宣告:
        段.append(宣告)
    新信 = _查收件(cwd, st) if not data.get("agent_id") else []
    if 新信:
        段.append(_收件提醒(新信, cwd))
        _log_trigger("收件", "PostToolUse", "、".join(n for n, _, _ in 新信), sid)
    if 耗时报:
        段.append(耗时报)      # ⭐ 已经是成品文案 ⇒ ⛔ 不过 _带许可（那是给条目 dict 用的）
    段 += [_带许可(e) for e in inject]
    # 🔁 常驻块重放：会话长到一定程度就把三句自问放回眼前（⛔ 不放原文，见 重放清单 的说明）
    #   ⭐ 排在**最后**：前面那些是「此刻发生了什么」（有时效），重放是「别忘了你是怎么干活的」。
    #   ⛔ 子代理不重放：它上下文短、且它的行为由主窗的单子约束。
    try:
        if not data.get("agent_id"):
            该, 现在多大 = _该重放吗(data, st)
            if 现在多大:
                st["上次重放字节"] = 现在多大 if 该 or not st.get("上次重放字节") else st["上次重放字节"]
            if 该:
                段.append(_填路径(重放清单, cwd))
                _log_trigger("常驻重放", "PostToolUse",
                             "转录 %.1f MB，距上次重放 ≥ %.1f MB"
                             % (现在多大 / 1048576, 重放字节 / 1048576), sid)
    except Exception:
        _log_error()
    if 段:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse", "additionalContext": "\n\n".join(段)}},
            ensure_ascii=False))
    _合并评估计数(评估增量)
    _state_save(sid, st)


def do_stop(data):
    if data.get("stop_hook_active"):
        return  # 平台若报「已被 Stop hook 拦过」⇒ 无条件放行（双保险之一）
    entries = _load_entries("Stop")
    if not entries:
        return
    sid = data.get("session_id")
    st = _state_load(sid)
    if st.get("blocks_total", 0) >= 2:
        return  # 全会话硬上限（双保险之二）
    # ⭐⭐ 2026-08-18 · codex 窗在真 codex 宿主上实测出来的（⛔ 非设想）：
    #   Codex 的 Stop 输入**直接给 `last_assistant_message`**（本轮最终回复），
    #   而这里原来先读 `transcript_path` —— Stop 到来时最终回复**还没稳定写进转录**
    #   ⇒ 转录为空 ⇒ 上面那道 `if not records: return` 直接退出
    #   ⇒ **JF-013 在 codex 上稳定漏拦**（挂点在跑、日志有、就是不叫）。
    # ⇒ 取值顺序：① 有 `last_assistant_message` 就用它（codex）
    #            ② 没有才退回转录（Claude 与旧宿主，行为⛔ 不变）
    # ⚠️ 搬这一段时**最关键的一步是删掉「转录为空就 return」**——
    #   ⛔ 留着它，字段给了也白给（codex 窗在回单里专门点了这一句）。
    records = _tail_records(data.get("transcript_path"))
    turn = _turn_slice(records) if records else []
    final_text = str(data.get("last_assistant_message") or "")
    if not final_text.strip():
        final_text = _final_assistant_text(turn)
    if not final_text.strip():
        return
    if _is_self_content(final_text):
        return  # 谈论纠偏库本身的回合不受纠偏正则管（自指防护，误报 A/B）
    match_text = _strip_quoted(final_text)  # 剥引用/代码：只匹配作者自己的陈述
    # ⭐ 与 codex 那版差一处，**这一处是本窗改的**：转录读不到时 `wrote` 取 **None（判不了）**，
    #   ⛔ 不是 False（＝确定没写过）。
    #   理由：`_cond_ok` 判的是 `wrote is not False` ⇒ 给 False 会让「本回合无写操作」
    #   这一类条目在**转录读不到**时照样弹（本仓 JF-004 承诺型收尾就靠它）
    #   ⇒ 那是**拿"判不了"冒充"确定没有"**，正是本库在治的病。
    #   给 None ⇒ 条件不成立 ⇒ 不弹（同 `_cond_ok` 文件头写的「判不了一律 False」）。
    #   ⚠️ 代价如实记：转录读不到那一轮，这类条目**不会响** —— 漏提醒好过假提醒。
    wrote = _wrote_this_turn(turn) if turn else None
    # W-2 复核修复（回单 §五 a）：Stop 也按“评估／条件挡／命中”记账，返回前只写一次。
    评估增量 = {}
    for e in entries:
        eid = str(e["id"])
        if st["counts"].get(eid, 0) >= 1:  # Stop 条目每会话只拦 1 次，⛔ 冷却字段改不动这条
            continue
        if not _scope_ok(e, data.get("cwd")):
            continue
        _记评估(评估增量, eid, "评估")
        m = re.search(str(e.get("触发正则") or ""), match_text)
        if not m:
            continue
        if not _cond_ok(e, {"wrote": wrote,
                            "改码没跑": _changed_code_without_running(turn),
                            "有派单配置": _has_dispatch_cfg(data.get("cwd")),
                            "派过单": bool(st.get("派过单"))}):
            _记评估(评估增量, eid, "条件挡")
            continue
        _记评估(评估增量, eid, "命中")
        st["counts"][eid] = st["counts"].get(eid, 0) + 1
        st["blocks_total"] = st.get("blocks_total", 0) + 1
        _log_trigger(eid, "Stop", match_text[max(0, m.start() - 60):m.end() + 60], sid)
        _合并评估计数(评估增量)
        _state_save(sid, st)
        print(json.dumps({"decision": "block",
                          "reason": _带许可(e)}, ensure_ascii=False))
        return  # 一次 Stop 只拦一条
    _合并评估计数(评估增量)
    _state_save(sid, st)


def do_user_prompt_submit(data):
    """⭐⭐ 用户话音刚落、模型还没开始想 —— **唯一读得到「用户说了什么」的挂点**。

    只管两件（⛔ 别再往里塞第三件：它每一轮用户输入都跑，是最贵的位置）：
      ① **说出「解限」即拿到身份**（用户 2026-08-17 第三次点名，前两轮都没真做成）
      ② **`事件: UserPromptSubmit` 的纠偏条目** —— 现在只有 JF-102（较真不是施压）：
         用户否定我的时候才响，⛔ 不再每窗常驻占位。

    ⛔ 本挂点**绝不 block**（⛔ 不许 exit 2）：拦用户的输入是最糟的失败模式——
      一旦正则写歪，用户会发现**自己说什么都发不出去**，而 fail-open 的哨连报错都不会响。
      ⇒ 只走 additionalContext（贴一段话，输入照常送出）。
    """
    sid = data.get("session_id")
    cwd = data.get("cwd")
    prompt = str(data.get("prompt") or "")
    st = _state_load(sid)
    段 = []
    评估增量 = {}

    # ① ⭐⭐ 解限：这是本挂点存在的理由
    try:
        意, 句 = _解限意图(prompt)
        我h = str(sid or "")
        授 = _读门铃授权()
        我是总管 = bool(授 and not 授.get("待认领") and str(授.get("hook会话") or "") == 我h)
        if 意 == "撤销" and 授:
            if 我是总管:
                try:
                    _门铃授权账().unlink()
                except OSError:
                    _log_error()
                段.append("✅ **总管模式已撤销**，五道闸立即恢复（本次共放行 %d 次门铃）。"
                          % int(授.get("已放行次数") or 0))
                _log_trigger("总管授权·口头撤销", "UserPromptSubmit", 句[:120], sid)
        elif 意 == "授权":
            if 我是总管:
                # ⭐ 已经是总管了 ⇒ ⛔ 不重开（重开会把已放行次数清零 ＝ 又一个假数字），
                #   只把那段话再贴一次：用户会重说一遍，多半正是因为他觉得我没收到。
                剩 = max(0.0, (float(授.get("到期时刻") or 0) - time.time()) / 3600)
                段.append("♻️ **你已经是总管了**（授权还在生效，⛔ 没有重开、用量没被清零）。\n\n"
                          + _总管模式话(授, 剩, "", 本次算一次=False))
                _log_trigger("总管授权·口头重申", "UserPromptSubmit", 句[:120], sid)
            else:
                d, 接管 = _落总管授权(data, 句)
                if d:
                    头 = "✅ **收到解限授权 —— 你从这一刻起就是本次任务的总管。**"
                    if 接管:
                        头 += ("\n⚠️ 原先有一份授权挂在**另一个窗**上（事由：%s），已改挂到本窗"
                               "——用户是在跟你说话，⇒ 总管只许有一个。"
                               % str((授 or {}).get("事由") or "?")[:40])
                    段.append(头 + "\n\n" + _总管模式话(d, 解限默认小时, "", 本次算一次=False)
                              + "\n\n⚠️ **这份授权是口头开的 ⇒ 还没扫过各窗用什么模型**"
                              "（用户的规矩：**Fable 不能这么烧**）。\n"
                              "⇒ 第一次要按铃叫别的窗时会被拦一次，按提示补两步即可"
                              "（`list_sessions` ＋ `get_session` ⇒ `门铃授权.py 补窗`）。\n"
                              "⭐ 写信、扫转录**现在就不受任何限制**。\n"
                              "⇒ 干完说一句「**%s**」，或 `门铃授权.py 撤`。" % 撤销口令)
                    _log_trigger("总管授权·口头开通", "UserPromptSubmit",
                                 ("接管｜" if 接管 else "") + 句[:120], sid)
        elif 意 == "疑似" and not st.get("疑似解限已问"):
            # ⭐⭐ ⛔ 沉默不许有两种含义：句式命中但句子偏长（像**在描述**授权，⛔ 不像在下令）
            #   ⇒ ⛔ 不自作主张开 10 小时全放开，但**也不能一声不吭**——
            #     否则用户以为说了就生效，而实际什么都没发生（正是他这次撞上的那个形状）。
            #   ⭐ 每会话只问一次：粘同一份文档三遍不该被问三遍。
            st["疑似解限已问"] = True
            段.append("ℹ️ 你这条消息里出现了解限/总管模式的字样，但它**⛔ 不是那句固定口令**"
                      "⇒ ⭐ **我没有自作主张开授权**。\n"
                      "⇒ 确实要授权我当总管，请**单独发这一句、一字不差**：\n"
                      "   **%s**\n"
                      "⚠️ 必须**整条消息就是这一句**（⛔ 前后别带别的字）——"
                      "这是 2026-08-17 收窄后的判据，为的是⛔ 不再把「讨论解限」误判成「授权解限」。\n"
                      "（撤销则说：**%s**。本提示每个会话只出现一次。）" % (授权口令, 撤销口令))
            _log_trigger("总管授权·疑似未落账", "UserPromptSubmit", 句[:120], sid)
    except Exception:
        _log_error()

    # ①b ⭐ 夜-3：用户说「我要睡了」⇒ 当场给睡前清单（用户 2026-08-17 点名）
    #   ⚠️ 本挂点文档头写着「⛔ 别再往里塞第三件」——我塞了，理由写在 `_睡前意图` 上方，
    #     ⛔ 不认那三条理由就把这段删掉，⛔ 不影响别的。
    #   ⭐ 每会话只响一次：⛔ 别让人说三遍睡觉就被问三遍（同「疑似解限」那条的处理）。
    #   ⚠️⚠️ **异常⛔ 不许静默吞掉**（2026-08-18 我自己刚踩：`_睡前话` 里有个 NameError，
    #     被外层的 fail-open 吃掉 ⇒ 钩子一声不吭 ⇒ **「没触发」和「触发了但崩了」长得一样**）。
    #   ⇒ ⭐ 判据：**触发之后必定有输出** —— 要么是清单，要么是「我崩了」。
    try:
        if _睡前意图(prompt) and not st.get("睡前已提"):
            st["睡前已提"] = True
            try:
                段.append(_睡前话(cwd))
            except Exception as _e:
                段.append("⛔⛔ **睡前清单生成时崩了**（⛔ 不是「没触发」）：%s: %s\n"
                          "⇒ 手动跑 `python 引擎/夜班.py 交接` 顶上，并把这条报给用户。"
                          % (type(_e).__name__, _e))
                _log_error()
            _log_trigger("夜班·睡前交接", "UserPromptSubmit", prompt[:60], sid)
    except Exception:
        _log_error()

    # ② UserPromptSubmit 事件的纠偏条目（目标固定为「用户输入」）
    try:
        文 = _strip_quoted(prompt)
        for e in _load_entries("UserPromptSubmit"):
            eid = str(e["id"])
            if st["counts"].get(eid, 0) >= int(e.get("冷却", 2)):
                continue
            if not _scope_ok(e, cwd):
                continue
            _记评估(评估增量, eid, "评估")
            if not 文 or _is_self_content(文):
                continue
            m = re.search(str(e.get("触发正则") or ""), 文)
            if not m:
                continue
            if not _cond_ok(e, {"有派单配置": _has_dispatch_cfg(cwd),
                                "派过单": bool(st.get("派过单"))}):
                _记评估(评估增量, eid, "条件挡")
                continue
            _记评估(评估增量, eid, "命中")
            st["counts"][eid] = st["counts"].get(eid, 0) + 1
            _log_trigger(eid, "UserPromptSubmit",
                         文[max(0, m.start() - 60):m.end() + 60], sid)
            段.append(_带许可(e))
    except Exception:
        _log_error()

    if 段:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "\n\n".join(段)}},
            ensure_ascii=False))
    _合并评估计数(评估增量)
    _state_save(sid, st)


def _同事册(cwd):
    """⭐ 开窗按 cwd 目录名查 `关系册.yaml`，把「你的同事是谁、改什么要想到谁」注入上下文。

    ⭐⭐ 立此机制（用户 2026-08-14）：「代码接口和联调是我的盲区，我也不知道什么时候该问谁通知谁……
      我希望以后不用我来记得问，而是它当场就考虑到」＋「我不一定能记住每单的身份，
      需要一个机制让它稳定就能让窗口认得谁是同事」。
    ⇒ **机械注入，⛔ 不靠模型自觉、⛔ 不靠用户开窗报身份、⛔ 不靠两台机目录名一致**（认别名）。
    ⛔ 项目不在册 ⇒ 静默（独行侠是常态；「在不在册」由 接入.py --检查 报，⛔ 不在这儿刷屏）。
    """
    try:
        # ⭐ 与上面三个目录同款式的环境变量覆盖：插件形态下同事册住在**可写且升级不丢**的
        #   数据目录里，⛔ 不在引擎目录（引擎目录路径带版本号，升级＝换目录）。
        f = Path(os.environ.get("WORLDBOOK_关系册") or (ROOT / "关系册.yaml"))
        if not f.is_file():
            return None
        import yaml
        册 = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        名下 = os.path.basename(os.path.normpath(str(cwd))).lower()
        本名 = None
        for 正名, 项 in (册.get("项目") or {}).items():
            # ⚠️⚠️ 2026-08-16 实测修：这里原来是 `别名 or [正名]` ——**写了别名就不认正名了**
            #   ⇒ 谁的 `别名:` 里漏写自己的目录名，那台机器上哨就**认不出自己**
            #   ⇒ 同事册**整段不注入**，而这跟「这个项目没有同事」长得一模一样（静默）。
            #   ⭐ 信.py 的同款匹配一直是 `别名 + [正名]` ⇒ **两个引擎口径本来就不一致**，现统一。
            别名 = [str(x) for x in ((项 or {}).get("别名") or [])]
            if 名下 in [x.lower() for x in 别名 + [正名]]:
                本名 = 正名
                break
        if not 本名:
            return None
        段 = []
        for 组 in (册.get("关系") or []):
            成员 = list((组 or {}).get("谁") or [])
            if 本名 not in 成员:
                continue
            others = [m for m in 成员 if m != 本名]
            if not others:
                continue
            段.append("· **%s**：%s\n  ⇒ %s"
                      % ("、".join(others), str(组.get("关系") or ""),
                         str(组.get("改动要想到") or "").strip()))
        if not 段:
            return None
        # ⭐⭐ 发信命令里的路径必须是**此刻真能敲的**，⛔ 不能留 `<垫片目录>` 这种占位。
        #   ⚠️⚠️ 2026-08-16 实测：插件形态下这里原样印着 `<垫片目录>` ⇒ 模型敲不出任何路径
        #     ⇒ **收信提醒照常响，而没有任何人发得出信**——半边机制成了装饰。
        #   ⇒ 依次取：插件铺的稳定工具目录 → 本项目的垫片目录 → 都没有才退回占位符。
        工具 = os.environ.get("WORLDBOOK_工具目录") or _垫片目录(cwd) or "<垫片目录>"
        return ("🤝【同事册】你在 **%s**。你的同事（⛔ 改动落定前先想一遍他们会不会被牵动，"
                "⛔ 不等用户来问「要不要联动」）：\n%s\n"
                "⭐ 要通知/求助 ⇒ `python %s/信.py 发 --给 <角色> --给项目 <上面的项目名> "
                "--题 … --级 黄 --从 \"本项目名＋角色\" --正文 <文件>`（项目名认别名，两台机目录名不同也投得到；"
                "--从 必填——不署名对方按不了门铃回追）。\n"
                "⭐ 缺对方窗才有的信息/判断 ⇒ 先扫对方转录（search_session_transcripts），"
                "扫不到可敲门（send_message，**优先对方项目最新活跃的窗**⛔ 不是挑空闲的旧窗；"
                "接不接/何时接由被敲方定；⛔ 敲门方不代改对方模块——谁的模块谁负责）。\n"
                "⭐ 敲门**第一行必标带宽档**：`[一句话]`/`[要查证]`/`[要联调]`/`[要协作决策]`"
                "（被敲方判断实际更大**有权改档**，⛔ 不算推诿）；**被排期 ⇒ 请求转成一封黄信**"
                "（带宽档＋上下文指针），⛔ 办完前不许标已读——靠信箱当队列，⛔ 不建排期表。"
                "——前提与成本权衡见本库 _dev/跨窗协作_怎么用.md §六。"
                % (本名, "\n".join(段), str(工具).replace("\\", "/")))
    except Exception:
        _log_error()
        return None


def _填路径(文, cwd=None):
    """把注入文本里的占位符换成**本机此刻算出来的真路径**。

    ⭐⭐ 2026-08-15 用户点名立此机制：「盘符写死问题已经犯过不止一次了」。
      入库门槛 4（注入文本必须通用措辞）此前只能靠**改措辞**去躲——
      但「候选写去哪」这类条目**必须说清落点**，越躲越不可用。
      ⇒ 正解⛔ 不是换个说法，是**根本不写**：条目里放占位符，引擎按本机算出来填。
      ⇒ 公司机填出 F 盘、家里机填出 G 盘，**同一份条目两台机都对**，
        而条目文件里**一个盘符、一个仓名都没有** ⇒ 门槛 4 从"靠自觉"变成"结构上做不到"。

    ⚠️ 认不出的占位符**原样留着**（⛔ 不清空）——留着的花括号看得见，
      而悄悄替换成空串会让那句话变成「写进 」这种读不懂的残句。
    """
    表 = {"{候选区}": STAGING_DIR.as_posix(),
          "{生效区}": ACTIVE_DIR.as_posix(),
          "{纠偏库}": ROOT.as_posix(),
          # ⭐ 插件形态下「工具住哪」由适配层算出来（那个路径带版本号，⛔ 条目里写不得）；
          #   非插件形态下退回项目的垫片目录。⇒ 同一条条目在两种形态下都指得准。
          # ⚠️ 2026-08-16 实测：最后一级原本印 `<垫片目录>` 这个尖括号占位记号
          #   ⇒ 在**库自己**身上（它故意不装垫片）会渲染成「去 <垫片目录> 里找脚本」
          #   ⇒ 一句**读不懂也照不做**的话。⭐ 占位记号是给写条目的人看的，
          #     ⛔ 不该漏到模型眼前。⇒ 兜底改成库自己的引擎目录（那儿真有脚本）。
          "{工具目录}": (os.environ.get("WORLDBOOK_工具目录")
                        or _垫片目录(cwd) or (ROOT / "引擎").as_posix()),
          "{配置目录}": 配置目录}
    for k, v in 表.items():
        文 = 文.replace(k, v)
    return 文


def do_session_start(data):
    lines = []
    cwd = data.get("cwd") or os.getcwd()
    # 常驻（蓝灯）条目：每窗开始原文注入。⛔ 须极少——它们不走触发、不占冷却，全靠入库审批把量
    try:
        for e in _load_entries("常驻"):
            if _scope_ok(e, cwd):
                lines.append(_填路径(str(e["注入文本"]).strip()))
    except Exception:
        _log_error()
    # 🤝 同事册：每窗开窗自动知道「我是谁的同事、改什么要想到谁」——⛔ 不靠用户记得问联动
    try:
        同 = _同事册(cwd)
        if 同:
            lines.append(同)
    except Exception:
        _log_error()
    # ⭐⭐ 总管授权：**开窗时也说一次**（2026-08-17 用户实测撞出的死锁，第二层保险）。
    #   ⚠️ 「认领即宣告」只覆盖**刚授权那一刻**；若那个窗**中途关掉又开**，
    #     它就再也不知道自己是总管 ⇒ 又回到死锁（不知道 ⇒ 不按铃 ⇒ 永远不被告知）。
    #   ⇒ ⭐ 开窗时若授权仍有效**且本窗就是总管**，把那段话再贴一次。
    #   ⛔ 只贴给总管本人（比 hook 会话号）——⛔ 别对所有窗广播，
    #     那会让别的窗**以为自己也是总管** ⇒ 比不提醒更糟。
    try:
        _授 = _读门铃授权()
        _我h = str(data.get("session_id") or "")
        if _授 and not _授.get("待认领") and _我h and str(_授.get("hook会话") or "") == _我h:
            _剩 = max(0.0, (float(_授.get("到期时刻") or 0) - time.time()) / 3600)
            lines.append("♻️ **你上次被授权为总管，授权还在生效**（重开窗后再提醒一次）\n\n"
                         # ⭐ 同上：开窗重提⛔ 不是按铃 ⇒ 用量⛔ 不许 +1
                         + _总管模式话(_授, _剩, "", 本次算一次=False))
            _log_trigger("总管授权·开窗重提", "SessionStart",
                         "事由 %s｜还剩 %.1f 小时" % (str(_授.get("事由") or "?"), _剩), _我h)
        # ⭐⭐⭐ 铃-3（2026-08-17 双路实证）：**被解限的从属窗，此前完全不知道自己被解限了。**
        #
        # ⚠️ 证据两条，⛔ 互不相干却同一结论：
        #   · 打包发布窗被叫醒后，报了它开窗那一轮收到的**全部**注入内容
        #     ——五段常驻纪律、同事册、触发日志提醒，**没有一句提到转授/解限/门铃状态**。
        #   · 我在影子账本里伪造一个「转授项目里的窗开口说话」跑 UserPromptSubmit
        #     ⇒ **输出完全空白**（`cat -A` 复核过，⛔ 不是被吞掉的空白字符）。
        # ⚠️ 而且**下半段也成立**：开发需求窗实测「主动去查也查不出」——
        #   `查` 只印 hook 会话号，而**一个窗读不到自己的 hook 号**
        #   ⇒ 它看得出「有个窗是总管」，⛔ 看不出那是不是自己。
        # ⇒ ⭐ 后果正是「装了没人用」那一格：**受益人不知道自己受了益 ⇒ 它不会去用这份权限。**
        #
        # ⭐⭐ 修法的关键（开发需求窗提的，⭐ 它是对的）：
        #   **模型查不到自己是谁，但引擎查得到** —— 钩子每次被调都攥着自己的 `session_id` 和 `cwd`。
        #   ⇒ ⛔ 别让从属窗「自己去查」，由引擎**主动告诉它**。
        # ⛔ 三条边界（⛔ 别越）：
        #   ① 只贴给**真的在解限项目里**的窗，⛔ 不广播——广播会让无关的窗以为自己也被解限了；
        #   ② **一个会话贴一次**（挂在 SessionStart）⇒ ⛔ 不会变成每次工具调用的噪音；
        #   ③ ⛔ 总管本人**不走这一支**（它上面已经拿到整段纲领了，⛔ 别贴两遍）。
        if _授 and not (_我h and str(_授.get("hook会话") or "") == _我h):
            _本项目 = Path(str(cwd or ".")).name
            _now = time.time()
            _跟们 = list(_授.get("跟随") or [])
            _到, _档 = 0.0, ""
            for _t in list(_授.get("转授") or []) + _跟们:
                try:
                    _e = float(_t.get("到期时刻") or 0)
                except (TypeError, ValueError):
                    continue
                if str(_t.get("项目") or "") == _本项目 and _now < _e and _e > _到:
                    _到, _档 = _e, ("实时跟随辅助" if _t in _跟们 else "短时转授")
            if _到:
                lines.append(
                    "⭐⭐ **你所在的项目此刻处于解限中**（%s）——还剩 **%.0f 分钟**。\n"
                    "事由：%s\n"
                    "⇒ **这段时间你按铃找总管⛔ 不受闸限**（⛔ 不用攒、⛔ 不用等冷却）。\n"
                    "⚠️ 但先想一下：门铃只在「对方必须立刻改变行为」时才值得用；"
                    "只是要告诉它一件事 ⇒ **写信**（信落盘不会丢，门铃会丢）。\n"
                    "⚠️ 而且实测过：**按铃当天 3 次、0 次真的唤醒对方** ——"
                    "消息会到，但⛔ 不会启动对方那一轮 ⇒ 要紧的话**务必同时写信**。"
                    % (_档, max(0.0, (_到 - _now) / 60), str(_授.get("事由") or "（没写事由）")))
                _log_trigger("解限告知·从属窗", "SessionStart",
                             "项目 %s｜%s｜还剩 %.0f 分钟" % (_本项目, _档, (_到 - _now) / 60), _我h)
    except Exception:
        _log_error()
    # ⭐ 心跳＋记录通路自检（⛔ 必须在最前：后面所有告警都建立在"记录是可信的"这个前提上）
    try:
        警 = _心跳(data.get("session_id"))
        if 警:
            lines.append(警)
    except Exception:
        _log_error()
    # ⭐ 开窗先看信箱：新窗一进来就知道别的窗给它留了什么，⛔ 不用靠读 STATE 全文自己推断
    try:
        st0 = _state_load(data.get("session_id"))
        新信 = _查收件(cwd, st0, 含绿=True)   # ⭐ 开窗是绿级（FYI）唯一的提醒时机
        if 新信:
            lines.append(_收件提醒(新信, cwd))
            _state_save(data.get("session_id"), st0)
    except Exception:
        _log_error()
    cfg_path = Path(cwd) / 配置目录 / "压缩闸.yaml"
    if cfg_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            for item in cfg.get("热层") or []:
                rel = item.get("文件")
                limit = int(item.get("上限行数", 0))
                if not rel or limit <= 0:
                    continue
                fp = Path(cwd) / rel
                if not fp.exists():
                    continue
                n = len(fp.read_text(encoding="utf-8", errors="replace").splitlines())
                if n > limit:
                    lines.append(
                        "⚠️ 压缩闸：%s 现 %d 行，超上限 %d（E-12）。\n"
                        "⇒ 本窗收工时做一次「降级」：最旧条目**原样剪切**进 archive/，主文件留一行索引＋指针。"
                        "⛔ 不是改写摘要——转述必失真。\n"
                        "⇒ 硬规矩：`> 用户原话：` 引用块只许整段搬运；DECISIONS.md（append-only）豁免不压。"
                        "全文：%s" % (rel, n, limit, ROOT / "规则" / "降级规则.md"))
        except Exception:
            _log_error()
    try:
        pending = [p.name for p in STAGING_DIR.glob("*.yaml")]
        if pending:
            lines.append("📥 纠偏库：staging/ 有 %d 条候选待用户批准（%s）。批准＝移入 active/。"
                         % (len(pending), "、".join(pending[:5])))
    except Exception:
        _log_error()
    # 无载体约定的哨兵化（JF-005 同案清出的两处）：日志积到阈值就提醒人来处理，⛔ 别指望谁记得
    try:
        tl = STATE_DIR / "触发日志.jsonl"
        if tl.exists():
            n = sum(1 for ln in tl.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
            if n >= 20:
                lines.append("📊 纠偏库：触发日志已积 %d 条，该做一次误报率盘点了（逐条判真/误触发；"
                             "盘点完把 _state/触发日志.jsonl 归档清空即复位提醒）。" % n)
        # ⭐⭐ 哨自己的健康哨（2026-08-13 补）：fail-open 让哨永远不弄坏会话，
        #    但它同时让**哨自己坏掉**也悄无声息——实证：错误日志攒了 563 条（552 条同一个编码 bug），
        #    采样丢了几百条、触发日志少记 15 次、会话状态被写坏重置，**五天没有任何人发现**。
        #    ⇒ fail-open 必须配「错误攒够了就喊人」，否则等于把故障扫进地毯下面。
        el = STATE_DIR / "错误.log"
        # ⛔ 这里⛔ 不许再加体积预筛：初版写了 `size > 2000` 当快路径，结果 25 条短错误（875 字节）
        #    被它整个挡掉 ⇒ 判据变成「错得够多**且**够长才告警」。⭐ 判据只有一个：**出错次数**。
        if el.exists():
            文 = el.read_text(encoding="utf-8", errors="replace")
            n = 文.count("---- 20")
            if n >= 20:
                末 = [l for l in 文.strip().splitlines() if l.strip()]
                lines.append("🚑 纠偏库：**哨自己出错 %d 次**（%s）。fail-open 只保证不弄坏你的会话，"
                             "⛔ 不代表哨在正常工作——请看 `_state/错误.log` 定位后清空复位。"
                             % (n, (末[-1] if 末 else "")[:70]))
        坏 = list(STATE_DIR.glob("会话_*.json.坏"))
        if 坏:
            lines.append("🚑 纠偏库：有 %d 个**损坏的会话状态**已留证（`_state/*.json.坏`）"
                         "——那些会话的冷却计数与标志曾被重置过，⛔ 别拿它们的统计当真。" % len(坏))
        cy = STATE_DIR / "实测采样.jsonl"
        if cy.exists() and not (STATE_DIR / "字段已确认").exists():
            n = sum(1 for ln in cy.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip())
            if n >= 30:
                # ⭐⭐ 铃-8：**归档量必须一起报**。只说「已积 N 条」会让人以为"样本就这些"，
                #   而实际超 1MB 时截过好几轮 —— 当天就有窗拿 304 条当基线，
                #   转头只剩 113 条，凭空少 191 条却**看不出发生过截断**。
                # ⛔ 门槛 30 和「字段已确认」这个关掉提醒的开关**逐字没动**（⛔ 别顺手改行为）。
                归, 归档数 = 0, 0
                try:
                    for f in sorted(_采样归档目录().glob("采样_*.jsonl")):
                        归档数 += 1
                        归 += sum(1 for ln in
                                  f.read_text(encoding="utf-8", errors="replace").splitlines()
                                  if ln.strip())
                except Exception:
                    归 = -1
                尾 = ("" if 归 == 0 else
                      ("；⚠️ **归档里还有 %d 条**（%d 个文件，超 1MB 时截下来的）"
                       "⇒ 拿它当基线前**先把归档一起算**" % (归, 归档数)) if 归 > 0 else
                      "；⚠️ 归档目录读不出来 ⇒ ⛔ 判不了历史样本还剩多少")
                lines.append("🔬 纠偏库：stdin 实测采样已积 %d 条%s，可回看确认各 hook 实际字段了"
                             "（确认后在 _state/ 建空文件「字段已确认」即不再提醒）。" % (n, 尾))
    except Exception:
        _log_error()
    if lines:
        print("\n".join(lines))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    data = _read_stdin()
    mode = mode or str(data.get("hook_event_name") or "")
    try:
        _dump_sample(mode, data)
        if mode == "PreToolUse":
            do_pre_tool_use(data)
        elif mode == "PostToolUse":
            do_post_tool_use(data)
        elif mode == "Stop":
            do_stop(data)
        elif mode == "SessionStart":
            do_session_start(data)
        elif mode == "UserPromptSubmit":
            do_user_prompt_submit(data)
    except Exception:
        _log_error()
    sys.exit(0)


if __name__ == "__main__":
    main()
