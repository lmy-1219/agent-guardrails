# -*- coding: utf-8 -*-
"""插件适配层 —— 四个挂点的统一入口（hooks.json 只指这一个文件）。

⭐ 这一层存在的唯一理由：**让引擎一个字都不用改**。
   引擎（哨.py 等）本来就支持用环境变量改三个目录（WORLDBOOK_STATE_DIR / _ACTIVE_DIR / _STAGING_DIR，
   回归测试一直是这么用的）⇒ 插件形态只要在跑引擎之前把这三个变量指对，引擎就原样可用。
   ⇒ 插件里的引擎是本仓引擎的**逐字节副本**，发布脚本有一条断言盯着"逐字节相同"。

⛔⛔ 状态**绝不能**落在插件目录里（2026-08-16 实测）：
   插件的安装路径**带版本号**（…/<插件>/<版本>/），升级＝装到新目录、旧目录标记待回收
   ⇒ 写进插件目录的账本/日志**升级即丢**，而且插件目录**是可写的** ⇒ 看着能写，写了也白写。
   ⇒ 官方给了每插件专属状态目录 `CLAUDE_PLUGIN_DATA`，在版本目录**外面**，升级不动它。

⚠️ 本文件路径**故意用 ASCII**（engine/entry.py，⛔ 不叫 引擎/入口.py）：
   只有它会作为**字符串出现在 hook 命令里**交给 shell。别人机器的 shell 代码页未知，
   中文路径在那儿解不开时，钩子会**静默失败**（哨是 fail-open）⇒ 护栏全瞎且无人察觉。
   ⇒ 把风险摁在这一个文件名上；其余文件仍是中文（它们只被 Python 打开，⛔ 不过 shell）。
"""
import json
import os
import runpy
import shutil
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # ⛔ 别删。真实 hook 环境里没有 PYTHONIOENCODING ⇒ Windows 按本地代码页编码
    #   ⇒ 中文的拦截理由写出去是 GBK 字节，平台按 UTF-8 解析不了 ⇒ **决定被静默丢弃、工具照跑**。
    #   （2026-08-16 实测踩过一次：现象是"钩子在跑、日志有记录、就是拦不住"。）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

插件根 = Path(__file__).resolve().parent.parent


def _状态家():
    """状态该落哪。⭐ 优先官方的每插件数据目录，⛔ 不落插件目录本身。"""
    d = os.environ.get("CLAUDE_PLUGIN_DATA")
    if d:
        return Path(d)
    # 退路：没跑在插件里（本地直接调、或平台版本较老）⇒ 落用户目录，⛔ 仍不落插件目录
    return Path.home() / ".claude" / "agent-guardrails-data"


def _播种(家):
    """把插件自带的示范条目**首次**复制到用户的生效区。

    ⭐ 只补缺失的文件，⛔ 不覆盖用户改过的——条目一旦入生效区就归用户所有，
       升级插件⛔ 不许动它（否则用户批准过的取舍会被一次升级抹掉）。
    ⚠️ 本函数每次工具调用都会被调到 ⇒ 有标记文件时立刻返回，⛔ 不做任何磁盘遍历。
    """
    标记 = 家 / ".已播种"
    if 标记.exists():
        return
    种子 = 插件根 / "种子条目"
    生效 = 家 / "active"
    生效.mkdir(parents=True, exist_ok=True)
    (家 / "staging").mkdir(parents=True, exist_ok=True)
    if 种子.is_dir():
        for p in 种子.glob("*.yaml"):
            目标 = 生效 / p.name
            if not 目标.exists():
                shutil.copy2(p, 目标)
    标记.write_text("已播种，⛔ 删掉它会导致下次开窗重新补齐缺失的示范条目\n", encoding="utf-8")


def _依赖齐吗():
    """引擎靠 pyyaml 读条目。⛔ 缺了它，护栏是**完全静默**地不工作。

    ⚠️⚠️ 哨.py 里那段是：
        try: import yaml
        except: return []        ← 一条条目都不加载，且**没有任何提示**
    ⇒ 对新装的人，症状是「装成功了、钩子在跑、日志也有、就是永远不叫」——
      而这跟「我没犯过规」长得一模一样。⭐ 这正是本套东西自己的头号纪律：
      **沉默不许有两种含义**。
    ⇒ 所以在这儿明着报出来，⛔ 不让它悄悄空转。
    """
    try:
        import yaml  # noqa: F401
        return True
    except Exception:
        return False


def 主():
    事件 = sys.argv[1] if len(sys.argv) > 1 else ""
    if not _依赖齐吗():
        # ⭐ 只在开窗时说一次。⛔ 不在每次工具调用时刷屏——噪音毁信用比不提醒更糟。
        # ⛔ 也不再往下跑引擎：缺 yaml 时它做不了正事，跑了反而会和这条消息抢标准输出。
        if 事件 == "SessionStart":
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext":
                    "⛔【agent-guardrails 没在岗】缺少 Python 的 pyyaml 包 ⇒ "
                    "**一条规矩都没加载**，护栏此刻完全不工作。\n"
                    "⇒ 装上它即可恢复：`python -m pip install pyyaml`（装完重开一个窗）。\n"
                    "⚠️ 在装好之前，⛔ 别把「没被拦过」当成「我没犯过规」。",
            }}, ensure_ascii=False))
        return 0

    家 = _状态家()
    try:
        家.mkdir(parents=True, exist_ok=True)
        _播种(家)
    except Exception:
        pass  # fail-open：播种失败也不许弄坏用户的会话

    # ⭐ 只在**没有显式设定**时才填，⛔ 不覆盖使用者自己的选择（也让回归测试能照常接管）
    os.environ.setdefault("WORLDBOOK_STATE_DIR", str(家 / "_state"))
    os.environ.setdefault("WORLDBOOK_ACTIVE_DIR", str(家 / "active"))
    os.environ.setdefault("WORLDBOOK_STAGING_DIR", str(家 / "staging"))

    哨 = 插件根 / "engine" / "哨.py"
    if not 哨.is_file():
        return 0                                   # fail-open
    sys.argv = [str(哨)] + sys.argv[1:]
    runpy.run_path(str(哨), run_name="__main__")   # ⛔ 同进程，不起子进程（每次工具调用都跑）
    return 0


if __name__ == "__main__":
    try:
        sys.exit(主())
    except SystemExit:
        raise                  # 引擎自己 sys.exit(0)/(2)，原样透传——⛔ 别吞，拦截靠它
    except Exception:
        sys.exit(0)            # fail-open 兜底
