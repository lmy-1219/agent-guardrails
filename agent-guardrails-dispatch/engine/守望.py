# -*- coding: utf-8 -*-
"""守望 —— 「别人动了我盯的东西 ⇒ 我醒来看一眼」的**门磁**。

⭐⭐ 这不是定时器。⛔ 别把它当心跳用。
    配合框架的 `Monitor` 工具跑：本脚本每输出一行，框架就把那一行**当成一次唤醒**送进窗口。
    ⇒ 没动静 ＝ 一行不输出 ＝ **模型 0 成本**（守夜的是这个 shell 进程，⛔ 不是模型的上下文）。

⚠️⚠️ 为什么⛔ 不用 `ScheduleWakeup`（实测教训，2026-08-13 执行窗那一轮）：
    那是闹钟——到点把整个窗叫醒、载入全部上下文、环顾一圈，**通常什么也没有**。
    该窗自己的结论：「五次发现 0 次由时钟产生」「空转一次和有收获一次成本一样，
    这是目前唯一没有闸的消耗口」。⇒ 病根在**形态**：闹钟天生要空转。
    ⭐ 换成门磁，空转这件事在结构上就不存在了。

⭐ 三条设计纪律（都是本项目撞出来的，⛔ 别删）：
  ① **布防/撤防各出一声**——⭐ 「没报」和「机制根本没起来」必须长得不一样。
     （本项目实证：一份触发日志"五天没动静"，实际是写盘在丢。⛔ 零记录 ≠ 没发生。）
  ② **醒来该干什么，写进事件那一行本身**——⛔ 不写进任何要人记得去读的文档。
     纪律放在文档里会被忘掉；放在唤醒词里，看见唤醒就必然看见它。
  ③ **只盯"别人的地盘"**——自己的目录用 `--除` 排掉。
     ⇒ 「谁写的谁不审」由**参数**保证，⛔ 不靠模型自觉。

跑法（一般由派单里的「并行守望」一节给出，⛔ 不靠模型现编）：
    python 守望.py --盯 _dev/STATE.md _dev/踩坑台账.md tools/ --除 tools/我的模块.py
"""
import argparse
import os
import stat
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ⛔ 这些目录永远不盯：机器自己就会改，盯了全是噪音
忽略目录 = {".git", "__pycache__", "node_modules", ".venv", "venv", ".trace",
            ".pytest_cache", "_scratch", "scratchpad", ".idea", ".vscode"}
忽略后缀 = {".pyc", ".pyo", ".swp", ".tmp", ".log", ".lock"}

# ⭐ 醒来清单：**跟着每一次唤醒一起送达**，⛔ 不指望谁去读文档
醒来清单 = ("⛔ 只读别改（两窗互改对方产物 ⇒ 归因链断）"
            "· ⛔ 别信总结去读产物"
            "· 无异常回一行「守望：无」即收，⛔ 别因为没事干就找活"
            "· 有异常写回单交用户裁，⛔ 不代改")


def 收集(盯, 除):
    """扫出 {路径: (mtime, size)}。⚠️ 扫的过程中文件可能被删/被占 ⇒ 逐个吞异常，⛔ 不许整体崩。"""
    快照 = {}
    # W-2 复核修复（回单 §二）：读失败仍要 fail-open，但必须留下能随下一声报出的数量。
    失败数 = 0
    除集 = [os.path.normcase(os.path.abspath(x)) for x in 除]

    def 记失败(_=None):
        nonlocal 失败数
        失败数 += 1

    def 该排除(p):
        n = os.path.normcase(os.path.abspath(p))
        return any(n == e or n.startswith(e + os.sep) for e in 除集)

    for 目标 in 盯:
        t = Path(目标)
        if 该排除(t):
            continue
        try:
            try:
                模式 = t.stat().st_mode
            except FileNotFoundError:
                continue   # ⚠️ 目标可待会儿才创建；不存在⛔ 不算“读不了”
            if stat.S_ISREG(模式):
                候选 = [t]
            elif stat.S_ISDIR(模式):
                候选 = []
                for 根, 子目录, 文件 in os.walk(t, onerror=记失败):
                    子目录[:] = [d for d in 子目录 if d not in 忽略目录]
                    if 该排除(根):
                        子目录[:] = []
                        continue
                    候选 += [Path(根) / f for f in 文件]
            else:
                continue   # ⚠️ 目标本身不存在：⛔ 不报错——它可能待会儿才被创建，那正是要抓的事件
            for f in 候选:
                if f.suffix.lower() in 忽略后缀 or 该排除(f):
                    continue
                try:
                    st = f.stat()
                    快照[str(f)] = (int(st.st_mtime), st.st_size)
                except OSError:
                    记失败()   # 扫到一半被删/被占，⛔ 不影响其余，但也⛔ 不再静默
        except OSError:
            记失败()
    收集.失败数 = 失败数
    return 快照


收集.失败数 = 0


def 短名(p):
    """报事件时把路径缩短一点。⚠️⚠️ ⛔ 别直接用 `os.path.relpath`——
    **监视对象和当前目录不在同一个盘时它直接抛 ValueError**（Windows），整个守望当场死掉。

    ⭐⭐ 这个 bug 是 2026-08-14 门磁首次实跑时炸出来的，而**它逃过了同一天的五项掺错演示**——
    因为那次测试给子进程设了 `cwd` 指向测试场，⇒ 盯的文件和 cwd 恰好同盘，
    `relpath` 自然不会抛。**自造夹具按被验对象的假设生成 ⇒ 必然通过**，这是活样本。
    ⇒ ⛔ 从此本文件的任何路径处理都要问一句：跨盘时它还成立吗？
    """
    try:
        r = os.path.relpath(p)
        return r if len(r) <= len(p) else p     # 跨目录时 relpath 可能反而更长（一堆 ..）
    except ValueError:
        return p                                 # 跨盘 ⇒ 老实用绝对路径，⛔ 不许崩


def 比(旧, 新):
    """返回 [(变化类型, 路径)]。⭐ 只报动了什么，⛔ 不猜是谁动的——mtime 里没有"谁"。"""
    出 = []
    for p in 新:
        if p not in 旧:
            出.append(("新增", p))
        elif 新[p] != 旧[p]:
            出.append(("改", p))
    for p in 旧:
        if p not in 新:
            出.append(("删", p))
    return 出


def 主():
    ap = argparse.ArgumentParser(description="守望：别人动了我盯的东西就出一声")
    ap.add_argument("--盯", nargs="+", required=True, help="要盯的文件/目录（别人的地盘）")
    ap.add_argument("--除", nargs="*", default=[], help="⭐ 排掉自己的地盘，⛔ 否则自己动自己醒")
    # ⚠️⚠️ 下面四个默认值的出处，⛔ 逐个如实交代（本项目正在治「253 处裸门槛」那个病，
    #    ⛔ 不许我自己再种四颗。⭐ 判据旁边写不出「凭什么是这个值」的，就明写"这是拍的"）：
    ap.add_argument("--隔", type=float, default=3.0,
                    help="几秒查一次。⭐ 框架文档对本地检查给的区间是 0.5~1 秒；"
                         "这里取 3 是**故意放慢**——守望不追求毫秒级，慢一点省 IO。⚠️ 上界⛔ 未实测")
    ap.add_argument("--上限", type=int, default=10,
                    help="⭐ 至多报几次就自撤（⛔ 硬数，⛔ 不是建议）。"
                         "10 的出处：用户 2026-08-14 派单原文「建议先取 10，跑几次再校」⇒ ⛔ 尚未校过")
    ap.add_argument("--冷却", type=float, default=25.0,
                    help="报过一次后至少隔这么久再报（把连改并成一声）。"
                         "⚠️ 25 是**拍的**，⛔ 无实测依据——真实使用后按「一次编辑会连报几声」回校")
    ap.add_argument("--最多列", type=int, default=6,
                    help="一声里最多点名几个文件。⚠️ 6 是**拍的**，⛔ 无依据，只为一行别太长")
    ap.add_argument("--谁", default="", help="可选：写明这是给哪个窗布的防，出现在每声里")
    a = ap.parse_args()

    基线 = 收集(a.盯, a.除)
    读不了 = 收集.失败数

    # ① 布防声。⭐⭐ 这一声是**通路证明**：看到它，之后的沉默才可信；
    #    ⛔ 没看到它 ＝ 机制压根没起来，此时"一直没报"什么都不能证明。
    谁 = f"[{a.谁}] " if a.谁 else ""
    # W-2 复核修复（回单 §二）：布防声把扫描盲区一并亮出来，沉默才有单一含义。
    读失败尾 = f" · 另有 {读不了} 处读不了" if 读不了 else ""
    print(f"🛡️ 守望已布防 {谁}· 盯 {len(基线)} 个文件（{len(a.盯)} 个目标，排除 {len(a.除)} 处）{读失败尾}"
          f"· 每 {a.隔:g} 秒查一次 · 至多报 {a.上限} 次后自撤 "
          f"⇒ ⭐ 这行是通路证明：⛔ 没有它就说明机制没起来，之后的「一直没动静」不作数",
          flush=True)

    # ⭐⭐ 空守望必须喊出来（2026-08-14 实跑撞出来的真 bug，⛔ 别删这段）：
    #   实测：拿 `--盯 _dev` 去布防两个⛔ 没有 _dev 目录的项目
    #   ⇒ 报「盯 **0** 个文件」然后**一切照常**运行 ⇒ **它永远不会叫**，
    #     而在使用者眼里，这跟「一直平安无事」长得**一模一样**。
    #   ⇒ ⭐ 这正是本项目的头号病（📖 踩坑台账「缺失被当成否定」／JF-004
    #     「没有」和「没查到」不是一回事）**出现在守望自己身上**。
    # ⛔ 为什么不直接退出：目标"现在还不存在、待会儿才被创建"是**合法用法**（收集() 里有注释），
    #    退出会把这种用法一起砍掉。⇒ **喊，但继续跑**——让失败可见就够了，⛔ 不必让它致命。
    不存在 = [t for t in a.盯 if not Path(t).exists()]
    if not 基线:
        print(f"⛔⛔ 守望警告 {谁}· **盯到 0 个文件** ⇒ 这次布防**永远不会叫**，"
              f"而「不会叫」和「平安无事」在你眼里长得一样。"
              + (f" 这些目标压根不存在：{'、'.join(不存在)} ⇒ ⭐ 多半是路径写错了。"
                 if 不存在 else " 目标存在但里面没有可盯的文件（⛔ 全被忽略规则或 --除 排掉了？）。")
              + " ⛔ 先把路径修对再重新布防，⛔ 别让它这么空转着。", flush=True)
    elif 不存在:
        print(f"⚠️ 守望提醒 {谁}· 有 {len(不存在)} 个目标现在不存在："
              f"{'、'.join(不存在)} ⇒ 若是**待创建**的东西，那没问题（创建时会报「新增」）；"
              f"⭐ 若是**写错的路径**，那这部分永远不会叫。⛔ 自己核一眼。", flush=True)

    # W-2 复核修复（回单 §二）：排除路径写错会让“谁写的谁不审”参数看似存在、实际永不生效。
    不存在的排除 = [t for t in a.除 if not Path(t).exists()]
    if 不存在的排除:
        print(f"⚠️ 守望提醒 {谁}· 有 {len(不存在的排除)} 个 --除 路径现在不存在："
              f"{'、'.join(不存在的排除)} ⇒ 这部分排除永远不会生效，多半是路径写错。"
              f"⛔ 自己核一眼。", flush=True)

    报过 = 0
    上次报 = 0.0
    # W-2 复核修复（回单 §一）：冷却内照常更新基线，但变化另存待报，按路径由后到覆盖。
    待报 = {}
    try:
        while True:
            time.sleep(a.隔)
            新 = 收集(a.盯, a.除)
            读不了 = 收集.失败数
            变 = 比(基线, 新)
            基线 = 新
            for t, p in 变:
                待报[p] = t
            if not 待报:
                continue
            if time.time() - 上次报 < a.冷却:
                continue   # ⭐ 冷却内的连改并进下一声，⛔ 别一个字一声
            上次报 = time.time()
            报过 += 1
            变 = [(t, p) for p, t in 待报.items()]
            待报.clear()
            名 = " ".join(f"{短名(p)}({t})" for t, p in 变[:a.最多列])
            余 = f" 等共 {len(变)} 处" if len(变) > a.最多列 else ""
            读失败尾 = f" · ⚠️ 另有 {读不了} 处读不了" if 读不了 else ""
            print(f"📣 守望｜{谁}别人动了你盯的：{名}{余}{读失败尾} ｜ {醒来清单}", flush=True)
            if 报过 >= a.上限:
                # ② 撤防声（触顶）。⛔ 不自行续命——一次布防 ＝ 一次预算。
                print(f"🛡️ 守望撤防：已报满 {a.上限} 次上限 ⇒ ⛔ 不自行续命。"
                      f"还要盯就重新布防（⭐ 先想清楚为什么这么吵）", flush=True)
                return 0
    except KeyboardInterrupt:
        print(f"🛡️ 守望撤防：被叫停 · 共报 {报过} 次", flush=True)
        return 0
    except Exception as e:
        # ⭐⭐ 兜住一切意外，并且**大声说出来**。⛔ 这不是"防御性编程"的客套：
        #   守望死掉的样子是「布防声之后再无动静」——**和"真的没人动"长得一模一样**。
        #   ⇒ 静静地死 ＝ 让沉默有了两种含义，那正是本套件反复要消灭的东西。
        #   （实证：2026-08-14 首次实跑，跨盘 relpath 抛异常当场猝死，
        #     若不是框架另外报了 failed，现场只会看到"布防了然后一直很安静"。）
        import traceback
        print(f"⛔⛔ 守望自己崩了（⛔ 不是「没人动」）：{type(e).__name__}: {e} "
              f"· 共报 {报过} 次 ⇒ ⭐ 把这行当异常处理，⛔ 别当成一切太平",
              flush=True)
        print("⛔ 崩溃现场：" + " | ".join(traceback.format_exc().splitlines()[-4:]), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(主())
