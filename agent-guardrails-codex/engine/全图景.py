# -*- coding: utf-8 -*-
"""全图景.py —— 生成一份「交给**框架外**审视者」的当前工作流全景说明。

⭐⭐ 为什么是生成器而不是一份文档（用户 2026-08-14 的场景）：
  用户有一个**Home 页签的 Agent 教学窗**，身份是**审视**（一开始就定死的），
  某些视角比施工窗更全。但实测 `list_sessions`（含归档）36 个会话**全是 Code 窗**
  ⇒ 那个窗在框架层面**够不着**（send_message / list_events 都到不了）。
  ⇒ 只能由人把说明**拷过去**。而人工维护的说明**当天就过期** ——
    本项目反复栽在「写在文档里＝以为它存在」上 ⇒ ⭐ 做成随时可重跑的生成器。

跑法：python <垫片目录>/全图景.py [--出 <文件>] [--简]
  默认写到 <项目>/_dev/全图景_给审视窗.md，⛔ 覆盖同名文件（它是派生物，⛔ 不是台账）。

⭐ 内容取向（⛔ 与给施工窗的交接件不同）：审视者要的不是"我们做了什么"，
  而是「**哪些是真验过的、哪些是自说自话、哪些从没被走到过、我们已知自己瞎在哪**」。
  ⇒ 全篇以**证据分档**与**已知盲区**为骨架，⛔ 不写成成果汇报。
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _项目根():
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env and (Path(env) / ".claude").is_dir():
        return Path(env).resolve()
    d = Path.cwd().resolve()
    for p in (d, *d.parents):
        if (p / ".claude").is_dir():
            return p
    return d


仓名候选 = tuple(n.strip() for n in
                (os.environ.get("WORLDBOOK_DIRNAMES") or "agent-guardrails").split(",") if n.strip())


def _找库(根):
    """找基建仓。⭐ 与 模板/垫片_哨.py 同口径，⛔ 一个盘符都不许出现（用户 2026-08-15 裁定）。

    ⚠️⚠️ 原来最后一级写死了某台机器的绝对路径。别的机器上那个目录**根本不存在**，
      而本函数找不到库是**静默返回 None** ⇒ 全图景在另一台机上一直是哑的，且没人会发现。
      ⭐ 用户原话：「写死的路径**不是兜底，是保证失效**」——垫片当时改对了，本文件漏了。
    ⇒ 三级：① 显式锚点（插件根 / WORLDBOOK 环境变量）
            ② 本机登记（在用户主目录，⛔ 不进任何仓 ⇒ 换机各写各的）
            ③ 从项目往上逐层找（这一层本身是库？这一层下面有同名目录？）
    """
    候 = []
    for k in ("CLAUDE_PLUGIN_ROOT", "WORLDBOOK"):
        if os.environ.get(k):
            候.append(Path(os.environ[k]))
    try:
        册 = Path.home() / ".claude" / "worldbook路径"
        if 册.is_file():
            for 行 in 册.read_text(encoding="utf-8", errors="replace").splitlines():
                行 = 行.strip()
                if 行 and not 行.startswith("#"):
                    候.append(Path(行).expanduser())
    except Exception:
        pass
    for i, p in enumerate((根, *根.parents)):
        if i > 8:                       # 够深了；stat 很便宜
            break
        候.append(p)
        候 += [p / n for n in 仓名候选]
    for c in 候:
        try:
            if (c / "哨.py").is_file():
                return c
        except OSError:
            pass
    return None


def _读yaml(p):
    try:
        import yaml
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _转录目录(根):
    """哨侧会话 UUID ＝ 转录文件名（2026-08-13 实测确认）⇒ 脚本能直接枚举会话。
    ⚠️ 但**标题与模型是 CCD 侧的**，脚本拿不到 ⇒ 用首条用户消息当代称，并如实标注这一点。"""
    d = Path.home() / ".claude" / "projects" / ("F--" + str(根).replace(":", "").replace("\\", "-").replace("/", "-").lstrip("-"))
    if d.is_dir():
        return d
    base = Path.home() / ".claude" / "projects"
    if base.is_dir():
        名 = 根.name.lower()
        for c in base.iterdir():
            if c.is_dir() and 名 in c.name.lower():
                return c
    return None


def _首条用户消息(f, 上限=160):
    try:
        with io.open(f, encoding="utf-8", errors="replace") as fh:
            for i, ln in enumerate(fh):
                if i > 400:
                    break
                if '"type": "user"' not in ln and '"type":"user"' not in ln:
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("isMeta") or r.get("isSidechain"):
                    continue
                c = (r.get("message") or {}).get("content")
                bs = c if isinstance(c, list) else ([{"type": "text", "text": c}] if isinstance(c, str) else [])
                for b in bs:
                    if isinstance(b, dict) and b.get("type") == "text":
                        t = re.sub(r"<[^>]+>", " ", str(b.get("text") or "")).strip()
                        t = re.sub(r"\s+", " ", t)
                        if len(t) > 12 and "Caveat" not in t:
                            return t[:上限]
    except Exception:
        pass
    return "（取不到首条用户消息）"


def 生成(根, 简=False):
    库 = _找库(根)
    L = []
    W = L.append
    now = time.strftime("%Y-%m-%d %H:%M")
    W("# 工作流全图景 · 给「审视窗」的交底")
    W("")
    W("> ⭐ **本文是脚本生成的**（`全图景.py`），⛔ 不是人写的说明——人写的当天就会过期。")
    W("> **生成时刻**：%s。⚠️ 越旧越不可信，拿到手先看这个时间。" % now)
    W("> ⭐ **给你（审视者）的定位**：下面刻意**不写成成果汇报**，而按")
    W("> 「**哪些真验过 / 哪些是自说自话 / 哪些从没被走到过 / 我们已知自己瞎在哪**」组织。")
    W("> ⇒ 你最该盯的是**最后两节**（未触发清单、已知盲区），⛔ 不是前面的架构图。")
    W("")
    W("---")
    W("")
    W("## 〇、你够不着的部分（先说清边界，免得你以为能联动）")
    W("")
    W("- 施工侧是 **Claude Code 窗口**，彼此之间有框架级通道："
      "`list_sessions`（谁在跑）· `list_events`（**直接读另一个窗的转录，对它零消耗**）·"
      "`search_session_transcripts`（全文搜哪个窗碰过某文件）· `send_message`（把消息作为一个"
      "用户回合推进对方，**会花掉对方一整轮**）。")
    W("- ⛔ **你（Home 页签的 Agent）不在这套名册里**——实测 `list_sessions` 含归档共列出的会话"
      "**全部带 cwd、全是 Code 窗**。⇒ 施工窗**无法**给你发消息，你也**无法**直接读它们。")
    W("- ⇒ 你与我们之间目前**只有人肉信道**：用户把本文拷给你，把你的意见拷回来。")
    W("  ⭐ 若你要回话，**按文末「回话模板」写**，用户贴回来后我们会当作一封正式来信处理。")
    W("")
    W("## 一、地图：仓、窗、职责")
    W("")
    W("**主项目**：`%s`" % 根)
    cl = 根 / "CLAUDE.md"
    if cl.is_file():
        t = cl.read_text(encoding="utf-8", errors="replace")
        一句 = re.search(r"^##\s*这是什么项目\s*\n+(.+?)\n", t, re.M | re.S)
        if 一句:
            W("> " + re.sub(r"\s+", " ", 一句.group(1))[:300])
    W("")
    W("**跨项目工作流基建仓**：`%s`" % (库 or "（没找到）"))
    W("  —— 纠偏库引擎（哨.py，挂 4 个 hook）＋条目库＋接入器＋跨窗信箱脚本＋codex 派单引擎。")
    W("  ⭐ **改一处、所有接入项目同时生效**；各项目只留薄配置与转发垫片。")
    W("")
    if 库:
        接入 = []
        for c in sorted((库.parent).iterdir()) if 库.parent.is_dir() else []:
            wf = c / ".claude" / "工作流.yaml"
            if c.is_dir() and wf.is_file():
                d = _读yaml(wf)
                接入.append((c.name, d.get("垫片目录") or "?", "有信箱" if (c / (d.get("收件目录") or "_dev/收件")).is_dir() else "无信箱"))
        if 接入:
            W("**已接入这套基建的项目**（＝配了 `.claude/工作流.yaml` 的）：")
            for n, s, x in 接入:
                W("  · `%s`（垫片目录 `%s`，%s）" % (n, s, x))
            W("")
    td = _转录目录(根)
    if td:
        fs = sorted(td.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
        W("**本项目最近活跃的会话**（脚本从转录文件枚举；"
          "⚠️ **标题与模型是框架侧信息、脚本拿不到**，这里用首条用户消息代称）：")
        for f in fs:
            W("  · `%s…` · %s · %.1f MB · %s" % (
                f.stem[:8], time.strftime("%m-%d %H:%M", time.localtime(f.stat().st_mtime)),
                f.stat().st_size / 1048576, _首条用户消息(f, 90)))
        W("")
    W("## 二、跨窗协作机制（今天刚建成的部分，⚠️ 大多**只跑过一天**）")
    W("")
    W("| 机制 | 干什么 | 载体 | 成熟度 |")
    W("|---|---|---|---|")
    W("| **门铃** | 把对方从等待里叫起来 | 框架 `send_message` | 实测通过（1 次真发真收） |")
    W("| **门铃调速器** | 5 道闸防止互相打断烧配额 | PreToolUse 拦 send_message | ⚠️ 只有单元测试，**真实拦截 0 次** |")
    W("| **跨窗信箱** | 耐久的定向留言（红/黄/绿三级） | 文件 ＋ 哨在工具后/开窗时提醒 | 实测通过（多封真信往返） |")
    W("| **撞车卫兵** | 写别人 30 分钟内动过的文件 ⇒ 拦一次自查 | PreToolUse | 真实拦过 1 次（拦对了） |")
    W("| **纠偏条目库** | 表面信号触发的当场纠偏 | 4 个 hook ＋ yaml 条目 | 见下节分档 |")
    W("")
    W("⭐ **今天确立的一条成本铁律**（审视时可用它检查我们有没有违反）：")
    W("> **读对方 transcript 是免费的**（`list_events`/`search_session_transcripts`/`get_session`"
      "对被读方零消耗），**按门铃要花对方一整轮**。")
    W("> ⇒ 凡「我需要信息」⇒ **去读**；门铃只配用于「**对方需要改变行为**」。")
    W("")
    W("## 三、⭐⭐ 证据分档（审视者请从这里开始）")
    W("")
    if 库:
        # 触发日志 = 唯一可信的命中统计（append-only）
        日志 = 库 / "_state" / "触发日志.jsonl"
        命中 = collections.Counter()
        心跳 = 0
        if 日志.is_file():
            for ln in 日志.read_text(encoding="utf-8", errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                if r.get("条目") == "心跳":
                    心跳 += 1
                else:
                    命中[r.get("条目")] += 1
        条目 = []
        for p in sorted((库 / "active").glob("*.yaml")):
            e = _读yaml(p)
            if e.get("id"):
                条目.append((str(e["id"]), str(e.get("名称", "")), str(e.get("事件", "")),
                             命中.get(str(e["id"]), 0)))
        跑过 = [c for c in 条目 if c[3] > 0]
        没跑过 = [c for c in 条目 if c[3] == 0 and c[2] != "常驻"]
        常驻 = [c for c in 条目 if c[2] == "常驻"]
        W("**纠偏条目库现状**：共 %d 条（其中常驻 %d 条）。" % (len(条目), len(常驻)))
        W("统计口径 ⭐ **只认 append-only 的触发日志**——"
          "会话状态里的计数会过期/被覆盖/损坏时重置，是残账（2026-08-13 实证两本账 38 vs 17）。")
        W("")
        W("**✅ 真实触发过的**（有日志为证）：")
        for i, n, ev, c in sorted(跑过, key=lambda x: -x[3]):
            W("  · `%s` %s（%s）—— %d 次" % (i, n, ev, c))
        W("")
        W("**⛔ 一次都没被走到过的**（⚠️ ＝**没验过**，⛔ 不算通过）：")
        for i, n, ev, c in 没跑过:
            W("  · `%s` %s（%s）" % (i, n, ev))
        W("")
        W("**（常驻条目不进触发日志，每窗开窗原文注入，因此上表看不到它们）**：%s"
          % "、".join("%s %s" % (i, n) for i, n, _, _ in 常驻))
        W("")
        W("⭐ **心跳**：开窗写一行心跳到触发日志（现有 %d 条）。" % 心跳)
        W("  ⇒ 判据：**有心跳无触发＝真没触发；连心跳都没有＝记录通路坏了**。")
        W("  （立此机制的事故：一份日志「五天没动静」被读成「条目没触发」，实为写盘在丢。）")
        W("")
        门 = 库 / "_state" / "门铃.jsonl"
        n门 = sum(1 for l in 门.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()) if 门.is_file() else 0
        错 = 库 / "_state" / "错误.log"
        n错 = 错.read_text(encoding="utf-8", errors="replace").count("---- 20") if 错.is_file() else 0
        W("**其它仪表**：门铃账本 %d 条 · 哨自身错误日志 %d 条（≥20 会在开窗喊人）。" % (n门, n错))
        W("")
    W("## 四、⛔ 我们已知自己瞎在哪（⭐ 审视者最该盯这里）")
    W("")
    W("1. **正则抓形态，⛔ 抓不住语义**。条目库只对「有表面信号」的病有效；"
      "真正危险的那次若不带那些词，条目照样漏。⇒ ⛔ 别把它当保险。")
    W("2. **撞车卫兵盲区**：只拦 `Write/Edit` 类工具；**Bash 重定向/heredoc 写入看不见**，"
      "外部程序（Photoshop/codex）产出的文件也看不见。")
    W("3. **资源级冲突**：撞车卫兵看**文件**、信箱看**留言**，两者都看不见"
      "「谁在用那台外部工具软件」。⇒ 已补两件：`哨.py` 的**占用卫兵**（抢同一个程序 ⇒ 拦一次）"
      "＋ `引擎/卡住哨.py`（**进程外**数时间，卡太久 ⇒ 当场喊人）。")
    W("   ⚠️⚠️ **一条归因已于 2026-08-15 撤回，⛔ 别再引用旧版**：那次「一步跨 10.5 小时」"
      "**不是**另一窗抢 Photoshop（那只是**共存事实**，⛔ 不构成因果），"
      "而是**那个程序弹了模态框在干等人点**——用户点掉之后进程才继续动（用户第一手，"
      "「点掉框→立刻恢复」是一次**干预**，证据强得多）。"
      "⇒ ⛔ 抢占那条没删（未被证实 ≠ 被证伪），但**排第二**。")
    W("   ⭐ 这一类**⛔ 不限于 Photoshop**：任何自带界面、由脚本驱动的程序"
      "（Illustrator / Unity / Blender / Excel·COM / 安装器与导出器）都会弹框等人，"
      "而**「在飞快地干活」和「弹了框干等你」从外面看长得一模一样：都是没返回**。"
      "⇒ 判据由**各项目自己声明**（`独占资源:` 的 `正常时长分钟`），⛔ 引擎不猜。")
    W("4. **「该去读对方」这件事没有触发器**：成本铁律说「该读」，但**什么时候想起来读、读哪里**"
      "目前仍靠人/模型自觉。⚠️ 这是用户 2026-08-14 亲自点破的缺口，**尚未解决**。")
    W("5. **测试大多用自造夹具**：回归断言里绝大多数是伪造的 stdin/transcript ——"
      "自造数据按被验对象的假设生成 ⇒ **必然通过**。真样本验过的只有少数几件。")
    W("6. **框架外的你**：我们无法给你发消息、你也读不到我们 ⇒ 你的介入完全依赖用户转达。")
    W("")
    W("## 五、⭐ 请你重点审的问题（我们自己判不了的）")
    W("")
    W("1. 这套「hook 拦一次 + 条目库 + 信箱 + 门铃闸」的**总体形态**，是不是在给一个"
      "**本该用别的办法解决的问题**打补丁？（例如：是不是本就不该开这么多并行窗？）")
    W("2. 我们用「**拦一次要求自查、原样重发即放行**」作为所有强制的统一形态。"
      "⛔ 它有没有系统性副作用——比如让模型学会「重发一次就过」从而形式化应付？")
    W("3. 条目库现在 %s 条，其中相当一部分从没触发过。"
      "**继续加条目**和**减少条目、改为少数强机制**，哪条路更对？"
      % (len(条目) if 库 else "?"))
    W("4. 我们大量使用「⛔/⭐/⚠️」符号与中文长注释来编码纪律。"
      "从**教学/可维护性**角度看，这是资产还是负债？")
    W("5. ⭐ **你视角比我们全的地方**：我们三个窗都在**同一个项目内部**看问题。"
      "有没有我们因为身处其中而系统性看不见的东西？")
    W("")
    W("## 六、回话模板（⭐ 用户会把你的回复贴回来，按这个格式写便于我们当正式来信处理）")
    W("")
    W("```")
    W("给：<工作流窗 / C-18 窗 / 执行窗 / 所有窗>")
    W("级：<红＝我们正在做的事前提就错了，会白干 | 黄＝有结论要交接 | 绿＝FYI>")
    W("题：<一句话>")
    W("正文：")
    W("  <你的意见。⭐ 若是纠错，请写清「你依据什么」——我们有一条硬纪律：")
    W("   自述不算证据、相容不等于支持。给判据比给结论有用。>")
    W("```")
    W("")
    W("---")
    W("")
    W("⭐ 本文可随时用 `python <垫片目录>/全图景.py` 重新生成，⛔ 不要基于旧版本讨论。")
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--出")
    ap.add_argument("--简", action="store_true")
    a = ap.parse_args()
    根 = _项目根()
    文 = 生成(根, a.简)
    出 = Path(a.出) if a.出 else (根 / "_dev" / "全图景_给审视窗.md")
    出.parent.mkdir(parents=True, exist_ok=True)
    出.write_text(文, encoding="utf-8")
    print("已生成：%s（%d 行）" % (出, len(文.splitlines())))
    print("⭐ 它是**派生物**：随时可重跑覆盖，⛔ 不用当台账维护、⛔ 不用入 git 也行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
