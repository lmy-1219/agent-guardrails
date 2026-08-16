# -*- coding: utf-8 -*-
"""codex派单.py —— 主窗派 codex 的唯一通道：派单 + 行为体检 + 子代理档位核账。
⭐ 2026-08-09 起为跨项目统一引擎（收编自四个项目各自的副本
   四份副本，逐条比对合并，实测坑注释全数保留）。项目差异全部外置到
   `<项目>/.claude/工作流.yaml`（预设表 / 真源根 / 特有闸 / 收尾哨附加），引擎⛔ 不含任何项目专有信息。

⭐⭐ 为什么有这个文件（2026-08-07 用户裁决）：
   「我希望的不是靠 codex 自觉……而是 claude 主模型在每次派 codex 时就根据任务性质
    给 codex 及其子代理做好档位规划并事后查验」「它得稳定起效而不是靠记忆」。

⛔ 所以主窗派 codex 一律走本脚本，⛔ 不许直接敲 `codex exec`：
   1. 派单时按【预设】定死：主模型+主档 / 子代理模型+档 / fork_turns / 沙箱，并把
      「子代理纪律」块自动追加进任务书（codex 漏做时，体检会抓出来）。
      模型、档位、沙箱**全部显式传**——治「不显式传＝按当时那台机器的默认档跑」
      （家里机默认 low、公司机默认 high 一类的机器差异，显式传参后不再依赖记忆兜底）。
   2. `codex exec --json` 后台可长跑、stdin 已关（⚠️ 实测坑：不关会等 stdin 挂住）。
   3. 跑完自动【体检】：读 `~/.codex/sessions` 的 rollout 原始记录（⛔ 不采信自述）。

⭐ 体检的能与不能（2026-08-07 实测边界，⛔ 别越界用）：
   ✅ 能判「有没有去做」：零检索交付 / 失败后收手 / 越界写 / 子代理档位违规 / token 轨迹
   ⛔ 不能判「做得对不对」：字段清单被当上限那类病（08-06 定位失败）它看不出来
   ⇒ ⛔ 体检永远只加在实跑验收【前面】，不取代实跑。

⚠️ 指标「检索位置覆盖」曾两次实现两次翻车（绝对路径匹配 / 命令文本数文件名），
   ⇒ 本版只【呈现事实】（跨出 cwd 去过哪些真源根），⛔ 不自动下 verdict——
   「该不该跨出去」要对照任务书，由主窗人判。

用法（经各项目旧路径的 shim 转发，老命令原样能用）：
  派：   python <shim> 派 --单 任务书.md --预设 <项目预设名> [--cwd DIR] [--主档 X] [--子档 X] [--fork none] [--沙箱 read-only] [--模型 M] [--子模型 M]
  体检： python <shim> 体检 --thread TID [--单 任务书.md]
  巡检： python <shim> 巡检 [--天 7]     ← 扫最近 rollout 的 spawn_agent 三参数合规
  配置： python <shim> 配置              ← 打印本项目生效的预设表/真源根/闸（接入自检用）
退出码：0=绿 · 2=黄（主窗自主追问）· 3=红（主窗自主打回，⛔ 不惊动用户）· 1=脚本自身出错
"""
import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SESSIONS = Path.home() / ".codex" / "sessions"

READ_RX = re.compile(
    r"(Get-Content|Select-String|Get-ChildItem|\bls\b|\bcat\b|grep|\brg\b|\bfind\b"
    r"|Test-Path|read_text|\btype\b|\bhead\b|\btail\b)", re.I)
WRITE_RX = re.compile(
    r"(Set-Content|Out-File|Add-Content|New-Item|Remove-Item|Move-Item|Copy-Item\s"
    r"|apply_patch|write_text|\bmkdir\b|\brm\b|\bmv\b|>\s*[^&|]|>>)", re.I)
FAIL_RX = re.compile(r"Exit code:\s*[1-9]|error|not found|无法|找不到|没有找到", re.I)

禁词RX = re.compile(r"⛔|禁止|不许|不碰|别碰")


def _剔禁区(文):
    """任务书文本 → 剔除禁区条款后的文本（只用于提取「点名的工作对象」）。
    ⚠️ 2026-08-14 实测误报（黄旗）：任务书【禁区】写「⛔ 不碰 ST 拷贝」，
    体检把『ST拷贝』当点名对象报「点名了但没碰」——可它恰恰是禁止碰的。
    ⇒ 剔两类：① 带禁令词的行；② 标题含「禁区/红线」的整节（到下一个标题为止）。
    ⛔ 剔多了只会少一面黄旗（主窗本来就要对照任务书人判），剔少了才是误报。"""
    出, 禁节 = [], False
    for l in 文.splitlines():
        if re.match(r"\s*(#{1,6}\s|【[^】]+】)", l):
            禁节 = bool(re.search(r"禁区|红线", l))
        if 禁节 or 禁词RX.search(l):
            continue
        出.append(l)
    return "\n".join(出)


def _写目标(c):
    """尽力从一条写类命令解析【写入目标路径】列表。解析不出 ⇒ 返回 []（调用方宁严处理：照旧升红）。
    ⚠️ 2026-08-14 实测误报（红旗，thread 01a0041e…）：真源闸按命令【全文】匹配，
    产物内容里引用一句真源路径就中红——而写产物到自己仓、内容提到素材路径是正常工作。
    ⇒ 判「写没写真源」看写到哪，⛔ 不看命令文本提没提。
    ⚠️ 命令常是 JSON/JS 转义过的一整行（实锤那条就是）⇒ 先还原 \\\\ 与 \\n 再抓；
    还原/解析失败只影响提取（提不出 ⇒ 宁严照红），⛔ 不会把真写放过去。"""
    s = c.replace("\\\\", "\x00").replace("\\n", "\n").replace("\\t", "\t").replace("\x00", "\\")
    出 = []
    for m in re.finditer(r"\*{3}\s*(?:Add|Update|Delete)\s+File:\s*([^\n\"']+)", s):
        出.append(m.group(1))
    for m in re.finditer(r"(?<![<>])>{1,2}\s*\"?([A-Za-z]:[\\/][^\s\"'<>|;&]+)", s):
        出.append(m.group(1))
    for m in re.finditer(r"(?:Set-Content|Out-File|Add-Content)\b[^\n;|]*?[\"']?([A-Za-z]:[\\/][^\s\"';|]+)", s, re.I):
        出.append(m.group(1))
    for m in re.finditer(r"\bopen\(\s*r?[\"']([^\"']+)[\"']\s*,\s*[\"'][wax]", s):
        出.append(m.group(1))
    return [t.strip().strip('"\'') for t in 出 if t.strip()]

# ══════════════════════════════════════════════════════════════
# ⭐ 项目配置：<项目>/.claude/工作流.yaml —— 预设表 / 真源根 / 特有闸 / 收尾哨附加
#    ⚠️ 子模型只能 sol / terra —— 2026-08-07 实测：spawn_agent 工具当场拒绝 luna
#    （codex 原话「仅支持 gpt-5.6-sol 和 gpt-5.6-terra」）⇒ 子代理降费靠【档位】，⛔ 不靠换小模型。
#    ultra ⛔ 任何预设都不给——要用得用户点头后手工跑。
# ══════════════════════════════════════════════════════════════
缺省预设 = {  # 没有项目配置时的保守兜底（终态应是每个项目都有自己的 工作流.yaml）
    "勘察": dict(主模型=None, 主档="low", 子模型="gpt-5.6-terra", 子档="low", fork="none", 沙箱="read-only"),
    "实现": dict(主模型=None, 主档="medium", 子模型="gpt-5.6-terra", 子档="low", fork="3", 沙箱="workspace-write"),
}


def 找项目根(起点):
    p = Path(起点 or os.getcwd()).resolve()
    for d in (p, *p.parents):
        if (d / ".claude").is_dir():
            return d
    return p


def 读配置(项目根):
    cfg = {"项目根": str(项目根), "预设": dict(缺省预设), "真源根": {}, "闸": [], "来源": "内置缺省"}
    f = Path(项目根) / ".claude" / "工作流.yaml"
    if not f.is_file():
        return cfg
    try:
        import yaml
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    except Exception as e:
        print(f"⚠️ 工作流.yaml 读取失败（{e}）⇒ 用内置缺省预设")
        return cfg
    if isinstance(d.get("预设"), dict) and d["预设"]:
        cfg["预设"] = d["预设"]
        cfg["来源"] = str(f)
    # 真源根：默认 ＋ 按主机覆盖（收编自 rp_* 的 platform.node() 分支——两台机器路径不同）
    根 = dict((d.get("真源根") or {}).get("默认") or {})
    按主机 = (d.get("真源根") or {}).get("按主机") or {}
    if platform.node() in 按主机:
        根 = dict(按主机[platform.node()] or {})
    cfg["真源根"] = {k: re.compile(v, re.I) for k, v in 根.items() if v}
    # 特有闸：判 ∈ 红 | 黄 | 写红读黄（收编自 ledgerkeep 真源ST闸 / citypulse main分支闸 / spec 盲测闸）
    for g in d.get("闸") or []:
        try:
            cfg["闸"].append(dict(名=g["名"], 正则=re.compile(g["正则"], re.I), 判=g.get("判", "黄"),
                                  红文=g.get("红文", ""), 黄文=g.get("黄文", "")))
        except Exception as e:
            print(f"⚠️ 闸「{g}」解析失败（{e}）⇒ 跳过")
    return cfg


def 找rollout(tid, 等秒=0):
    """按 thread_id 定位 rollout 文件。文件落盘可能略滞后 ⇒ 可等。"""
    截止 = time.time() + 等秒
    while True:
        for p in SESSIONS.rglob(f"*{tid}*.jsonl"):
            return p
        if time.time() >= 截止:
            return None
        time.sleep(2)


def 读rollout(path):
    """rollout → 事实包。⛔ 只提取，不判断。"""
    r = dict(meta={}, roots=[], model=None, effort=None, toks=[], cmds=[],
             outs=[], spawns=[], final="", patches=0, tid=None)
    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        pl = d.get("payload") or {}
        t = pl.get("type") or d.get("type")
        if t == "session_meta":
            r["meta"] = pl
            r["tid"] = pl.get("id")
        elif t == "turn_context":
            r["roots"] = pl.get("workspace_roots") or r["roots"]
            r["model"] = pl.get("model") or r["model"]
            r["effort"] = pl.get("effort") or r["effort"]
        elif t == "token_count":
            tt = (pl.get("info") or {}).get("total_token_usage") or {}
            if tt.get("total_tokens"):
                r["toks"].append(tt["total_tokens"])
        elif t in ("custom_tool_call", "local_shell_call"):
            s = pl.get("input") or pl.get("arguments") or ""
            r["cmds"].append(s if isinstance(s, str) else json.dumps(s, ensure_ascii=False))
        elif t == "function_call":
            nm = str(pl.get("name") or "")
            a = pl.get("arguments") or pl.get("input")
            if isinstance(a, str):
                try:
                    a = json.loads(a)
                except Exception:
                    a = {}
            if "spawn_agent" in nm and isinstance(a, dict):
                r["spawns"].append(a)
            else:
                r["cmds"].append(json.dumps(a, ensure_ascii=False) if isinstance(a, dict) else str(a))
        elif t in ("custom_tool_call_output", "function_call_output"):
            o = pl.get("output")
            r["outs"].append(o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)[:600])
        elif t == "patch_apply_end":
            r["patches"] += 1
        elif t == "agent_message":
            r["final"] = pl.get("message") or pl.get("text") or r["final"]
    return r


def 找子代理(tid):
    """全库扫 source.subagent.thread_spawn.parent_thread_id == tid 的子线程。"""
    子 = []
    for p in SESSIONS.rglob("rollout-*.jsonl"):
        try:
            head = "".join(l for _, l in zip(range(6), open(p, encoding="utf-8", errors="replace")))
        except Exception:
            continue
        if tid not in head:
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            pl = d.get("payload") or {}
            if (pl.get("type") or d.get("type")) != "session_meta":
                continue
            ts = ((pl.get("source") or {}).get("subagent") or {}).get("thread_spawn") \
                if isinstance(pl.get("source"), dict) else None
            if ts and ts.get("parent_thread_id") == tid:
                子.append(dict(file=p, depth=ts.get("depth"), nick=ts.get("agent_nickname")))
            break
    return 子


def 体检(cfg, tid, 单文件=None, 期望=None):
    """行为体检。期望 = dict(主档, 子档, 子模型, fork, 沙箱)——派单时给，独立体检可缺省。"""
    p = 找rollout(tid, 等秒=10)
    if not p:
        print(f"⛔ 找不到 thread {tid} 的 rollout（查了 {SESSIONS}）")
        return 1
    r = 读rollout(p)
    reads = [c for c in r["cmds"] if READ_RX.search(c)]
    writes = [c for c in r["cmds"] if WRITE_RX.search(c) and not READ_RX.search(c)]
    fails = [i for i, o in enumerate(r["outs"]) if FAIL_RX.search(o)]
    续查 = (len(r["cmds"]) - 1 > fails[0]) if fails else None
    blob = "\n".join(r["cmds"])
    跨根 = [k for k, rx in cfg["真源根"].items() if rx.search(blob)]

    红, 黄, 行 = [], [], []
    行.append(f"rollout   {p.name}")
    行.append(f"模型/档   {r['model']} · {r['effort']}    授权根 {r['roots']}")
    if 期望 and 期望.get("主档") and r["effort"] and r["effort"] != 期望["主档"]:
        红.append(f"实际档位 {r['effort']} ≠ 派单要求 {期望['主档']}（配置没生效，先修通道再谈内容）")
    if str(r["effort"]) == "ultra" and (not 期望 or 期望.get("主档") != "ultra"):
        红.append("跑在 ultra 上，且主窗没批过 ultra")

    # ① 去没去做 —— ⚠️ 父代理把活派给子代理时，读发生在子层 ⇒ 必须把子代理的读算进来
    #    （2026-08-07 实测误报：测试单明令父不许读、子去数文件，父层读=0 被误判红灯）
    子读 = 0
    子 = 找子代理(tid) if r["spawns"] else []
    子r = []
    for c in 子:
        cr = 读rollout(c["file"])
        子r.append((c, cr))
        子读 += len([x for x in cr["cmds"] if READ_RX.search(x)])
    if not reads and 子读 == 0 and r["final"]:
        红.append(f"零检索交付：命令 {len(r['cmds'])} 条、读类 0（含子代理），却交了 {len(r['final'])} 字符结论")
    行.append(f"① 检索     命令 {len(r['cmds'])} / 读类 {len(reads)}"
              + (f"（＋子代理读 {子读}）" if 子 else ""))
    # ② 只呈现事实，⛔ 不下 verdict（此指标两次实现两次翻车，降级为人判）
    行.append(f"② 跨出cwd  {('、'.join(跨根)) if 跨根 else '没有——全程在仓库内'}"
              f"    ⚠️ 该不该跨出去要对照任务书，主窗人判")
    if 单文件 and Path(单文件).is_file():
        单文 = Path(单文件).read_text(encoding="utf-8", errors="replace")
        # ⭐ 先剔禁区条款再提「点名对象」——禁区里点的名是「禁止碰」，⛔ 不是「该碰没碰」
        单文许 = _剔禁区(单文)
        要跨 = [k for k, rx in cfg["真源根"].items() if rx.search(单文许)]
        漏 = [k for k in 要跨 if k not in 跨根]
        if 漏:
            黄.append(f"任务书点名了 {要跨}，但没碰过：{漏} ← 逐格点名让它补跑")
    # ②b 项目特有闸（配置驱动；收编自 ledgerkeep 真源ST / citypulse main分支 / spec 盲测隔离）
    for g in cfg["闸"]:
        碰 = [c for c in r["cmds"] if g["正则"].search(c)]
        if not 碰:
            continue
        if g["判"] == "写红读黄":
            写 = [c for c in 碰 if WRITE_RX.search(c)]
            # ⭐ 2026-08-14 误报修正：红只按【写入目标路径】判，⛔ 不按命令全文。
            #   目标解析得出且全在闸外 ⇒ 降黄（附目标，主窗一眼核）；解析不出 ⇒ 照旧红（宁严勿松）。
            真写, 旁写 = [], []
            for c in 写:
                目标 = _写目标(c)
                if 目标 and not any(g["正则"].search(t) for t in 目标):
                    旁写.append(目标)
                else:
                    真写.append(目标)
            if 真写:
                标注 = "；".join(("、".join(t) if t else "（未解析出，翻 rollout 人工核）") for t in 真写)
                红.append((g["红文"] or f"⛔⛔ 触碰「{g['名']}」且含写动作").format(n=len(真写))
                          + f"｜写目标：{标注}")
            if 旁写:
                黄.append(f"「{g['名']}」字样出现在 {len(旁写)} 条写命令的【文本】里，但写目标都在别处："
                          + "；".join("、".join(t) for t in 旁写)
                          + " ——多半是产物内容引用了该路径，主窗一眼核即可")
            if not 写:
                黄.append((g["黄文"] or f"读了「{g['名']}」").format(n=len(碰)))
        elif g["判"] == "红":
            红.append((g["红文"] or f"⛔⛔ 触碰「{g['名']}」").format(n=len(碰)))
        else:
            黄.append((g["黄文"] or f"触碰「{g['名']}」").format(n=len(碰)))
    # ③ 失败后收没收手
    if 续查 is False:
        红.append(f"第 {fails[0]+1} 条命令失败后再无任何动作（近因压先验的形状）")
    行.append(f"③ 失败     {len(fails)} 次" + ("，之后仍在继续查" if 续查 else "，无失败" if 续查 is None else ""))
    # ④ 写/查比（只呈现）
    行.append(f"④ 产出     final {len(r['final'])} 字符 ÷ 读 {max(1,len(reads))} 次 = {len(r['final'])//max(1,len(reads))}")
    # ⑤ 只读边界
    if 期望 and 期望.get("沙箱") == "read-only" and (writes or r["patches"]):
        红.append(f"read-only 单里出现写动作：写类命令 {len(writes)} 条 / patch {r['patches']} 次")
    行.append(f"⑤ 写边界   写类命令 {len(writes)} / patch {r['patches']}")
    # ⑥ 子代理三参数核账
    for i, a in enumerate(r["spawns"], 1):
        缺 = [k for k in ("model", "reasoning_effort", "fork_turns") if not a.get(k)]
        if 缺:
            红.append(f"spawn_agent #{i}（{a.get('task_name')}）缺参数 {缺} ⇒ 子代理会继承父档")
        if a.get("fork_turns") == "all":
            黄.append(f"spawn_agent #{i}（{a.get('task_name')}）fork_turns=all ⇒ 整条历史复制给子代理")
        if 期望 and a.get("reasoning_effort") and 期望.get("子档") \
                and a.get("reasoning_effort") != 期望["子档"]:
            黄.append(f"spawn_agent #{i} 子档 {a.get('reasoning_effort')} ≠ 规划 {期望['子档']}")
    行.append(f"⑥ 子代理   spawn_agent {len(r['spawns'])} 次")
    for c, cr in 子r:
        标 = "🔴 ultra" if cr["effort"] == "ultra" else cr["effort"]
        行.append(f"    └ depth={c['depth']} {c['nick'] or ''}  {cr['model']} · {标}  "
                  f"token终值 {cr['toks'][-1] if cr['toks'] else '?'}")
        if cr["effort"] == "ultra" and (not 期望 or 期望.get("子档") != "ultra"):
            红.append(f"子代理 {c['nick']} 实际跑在 ultra（继承了父档）")
    # ⑦ token 轨迹
    tk = r["toks"]
    行.append(f"⑦ token    {len(tk)} 个决策点：{tk[:3]}…{tk[-2:] if len(tk)>4 else tk[3:]}"
              if len(tk) > 5 else f"⑦ token    {tk}")

    print("═" * 62)
    print(f"行为体检 · thread {tid}")
    for x in 行:
        print("  " + x)
    if 红:
        print("  🔴 红（主窗自主打回，指令里必须逐条点名下面这些）：")
        for x in 红:
            print("     · " + x)
    if 黄:
        print("  🟡 黄（主窗自主追问，⛔ 不惊动用户）：")
        for x in 黄:
            print("     · " + x)
    if not 红 and not 黄:
        print("  🟢 绿 —— 行为面过。⚠️ 体检不验对错，接着走产物验收（退出码→产物→实跑）")
    print("═" * 62)
    return 3 if 红 else (2 if 黄 else 0)


def 子代理纪律块(子模型, 子档, fork):
    return f"""

――――――――――――――――――――
【子代理纪律 · 主窗强制（⛔ 违规会被 rollout 逐条核出）】
1. 若你调用 spawn_agent / followup_task：每次必须显式带三个参数
   model="{子模型}"  reasoning_effort="{子档}"  fork_turns="{fork}"
   ⛔ 缺任一项 = 子代理继承你的模型和档位 = 违规。
2. 派发前先跑一条 shell 把任务书回显成一行 JSON（本地审计唯一明文来源）：
   node -e "console.log(JSON.stringify({{spawnBrief:{{target:'<task_name>',message:<message原文>}}}}))"
   （无 node 用 python -c 输出同构 JSON 也行。）
3. 抓取/核对类子代理 ⛔ 不得再向下派第二层。
"""


def 派(cfg, a):
    表 = cfg["预设"]
    if a.预设:
        if a.预设 not in 表:
            print(f"⛔ 预设「{a.预设}」不在本项目配置里（有：{'、'.join(表)}）——查 {cfg['来源']}")
            return 1
        p = 表[a.预设]
        主模型, 主档 = p.get("主模型"), p.get("主档", "low")
        子模型, 子档 = p.get("子模型", "gpt-5.6-terra"), p.get("子档", "low")
        fork, 沙箱 = str(p.get("fork", "none")), p.get("沙箱", "read-only")
    else:
        # ⚠️ 缺省子模型必须 terra——旧版此处曾写 luna，而 spawn_agent 当场拒绝 luna
        主模型, 主档, 子模型, 子档, fork, 沙箱 = None, "low", "gpt-5.6-terra", "low", "none", "read-only"
    主模型 = a.模型 or 主模型
    主档 = a.主档 or 主档
    子档 = a.子档 or 子档
    子模型 = a.子模型 or 子模型
    fork = a.fork or fork
    沙箱 = a.沙箱 or 沙箱
    if "ultra" in (主档, 子档):
        print("⛔ ultra 不许由脚本派出（要用 ultra 得用户点头后手工跑）")
        return 1
    单 = Path(a.单).read_text(encoding="utf-8", errors="replace") if Path(a.单).is_file() else a.单
    prompt = 单 + 子代理纪律块(子模型, 子档, fork)
    exe = shutil.which("codex")
    if not exe:
        print("⛔ 找不到 codex 可执行文件")
        return 1
    cmd = [exe, "exec", "--json", "-c", f"model_reasoning_effort={主档}",
           "-s", 沙箱, "--skip-git-repo-check", "-C", a.cwd or os.getcwd()]
    if 主模型:
        # ⭐ 模型显式传（收编自各项目旧版）；主模型=None 表示沿用该机 codex 默认（部分机器的现状）
        cmd += ["-m", 主模型]
    # ⚠️⚠️ 任务书必须走 stdin（`codex exec -`），⛔ 不许放 argv：
    #    本机 codex 是 npm 的 .CMD 垫片，带换行的中文 prompt 当参数传会被 cmd.exe
    #    撕碎（实测报 os error 2）。走 stdin 顺带根治「codex 等 stdin 挂住」的坑。
    cmd.append("-")
    print(f"派单：{主模型 or '(机默认)'}·{主档} 沙箱={沙箱} 子代理={子模型}·{子档}·fork={fork}")
    日志 = Path(a.日志) if a.日志 else None

    def 跑一次():
        tid = None
        fh = open(日志, "w", encoding="utf-8") if 日志 else None
        起 = time.time()
        with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace") as proc:
            proc.stdin.write(prompt)
            proc.stdin.close()
            for line in proc.stdout:
                if fh:
                    fh.write(line)
                    fh.flush()
                s = line.strip()
                if s.startswith("{"):
                    try:
                        d = json.loads(s)
                    except Exception:
                        continue
                    if d.get("type") == "thread.started":
                        tid = d.get("thread_id")
                        print(f"thread_id: {tid}", flush=True)
                    elif d.get("type") == "turn.completed":
                        print(f"usage: {json.dumps(d.get('usage'), ensure_ascii=False)}", flush=True)
                    elif d.get("type") == "item.completed" and (d.get("item") or {}).get("type") == "agent_message":
                        print("── codex 回报 ──")
                        print((d["item"].get("text") or "")[:4000], flush=True)
                elif s:
                    print("  [codex] " + s[:300], flush=True)   # ⭐ 启动期报错别吞掉
        if fh:
            fh.close()
        return tid, time.time() - 起

    tid, 耗 = 跑一次()
    if not tid and 耗 < 15:
        # ⚠️ 实测见过瞬时故障：codex 秒退报 os error 2，几十秒后自愈 ⇒ 只重试这一种
        print("⚠️ codex 秒退且无 thread_id ⇒ 等 5s 重试一次")
        time.sleep(5)
        tid, 耗 = 跑一次()
    if not tid:
        print("⛔ 没拿到 thread_id（codex 启动失败，上面 [codex] 行是它的原话）")
        return 1
    print()
    return 体检(cfg, tid, 单文件=(a.单 if Path(a.单).is_file() else None),
               期望=dict(主档=主档, 子档=子档, 子模型=子模型, fork=fork, 沙箱=沙箱))


def 巡检(a):
    """最近 N 天全部 rollout 的 spawn_agent 三参数合规表——抓「手动窗口漏配」。"""
    截止 = time.time() - a.天 * 86400
    坏 = 0
    总 = 0
    for p in sorted(SESSIONS.rglob("rollout-*.jsonl")):
        if p.stat().st_mtime < 截止:
            continue
        r = 读rollout(p)
        for s in r["spawns"]:
            总 += 1
            缺 = [k for k in ("model", "reasoning_effort", "fork_turns") if not s.get(k)]
            if 缺 or s.get("fork_turns") == "all" or s.get("reasoning_effort") in (None, "ultra", "xhigh"):
                坏 += 1
                print(f"⚠️ {p.name[:40]}… task={s.get('task_name')} "
                      f"model={s.get('model')} effort={s.get('reasoning_effort')} fork={s.get('fork_turns')}"
                      f"{' 缺'+str(缺) if 缺 else ''}")
    print(f"\n最近 {a.天} 天 spawn_agent 共 {总} 次，其中 {坏} 次不合规（缺参/ultra/xhigh/fork=all）")
    return 0 if 坏 == 0 else 2


def 打印配置(cfg):
    print(f"项目根：{cfg['项目根']}")
    print(f"配置来源：{cfg['来源']}    主机：{platform.node()}")
    print("预设表：")
    for k, p in cfg["预设"].items():
        print(f"  {k:6s} 主={p.get('主模型') or '(机默认)'}·{p.get('主档')}  "
              f"子={p.get('子模型')}·{p.get('子档')}  fork={p.get('fork')}  沙箱={p.get('沙箱')}")
    print(f"真源根：{'、'.join(cfg['真源根']) or '（无）'}")
    print(f"特有闸：{'、'.join(g['名'] + '(' + g['判'] + ')' for g in cfg['闸']) or '（无）'}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--项目", help="项目根（缺省＝从当前目录向上找 .claude）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("派")
    p1.add_argument("--单", required=True, help="任务书文件路径，或直接给文本")
    p1.add_argument("--预设")
    p1.add_argument("--cwd")
    p1.add_argument("--主档")
    p1.add_argument("--子档")
    p1.add_argument("--子模型")
    p1.add_argument("--fork")
    p1.add_argument("--沙箱")
    p1.add_argument("--模型")
    p1.add_argument("--日志", help="事件流落到哪个文件（可选）")
    p2 = sub.add_parser("体检")
    p2.add_argument("--thread", required=True)
    p2.add_argument("--单")
    p3 = sub.add_parser("巡检")
    p3.add_argument("--天", type=int, default=7)
    sub.add_parser("配置")
    a = ap.parse_args()
    cfg = 读配置(找项目根(a.项目 or (a.cwd if getattr(a, "cwd", None) else None)))
    if a.cmd == "派":
        sys.exit(派(cfg, a))
    if a.cmd == "体检":
        sys.exit(体检(cfg, a.thread, 单文件=a.单))
    if a.cmd == "巡检":
        sys.exit(巡检(a))
    if a.cmd == "配置":
        sys.exit(打印配置(cfg))


if __name__ == "__main__":
    main()
