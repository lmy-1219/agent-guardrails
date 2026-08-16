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
    # ⭐ 同事册模板：放进数据目录（可写、升级不丢）。默认全是注释 ⇒ 功能关着，
    #   这是故意的——独行侠是常态，⛔ 不该硬塞一份假名单。
    册 = 插件根 / "关系册.yaml.示例"
    if 册.is_file() and not (家 / "关系册.yaml").exists():
        shutil.copy2(册, 家 / "关系册.yaml")
    标记.write_text("已播种，⛔ 删掉它会导致下次开窗重新补齐缺失的示范条目\n", encoding="utf-8")


垫片模板 = '''# -*- coding: utf-8 -*-
"""稳定入口 —— 转发到**此刻这一版**的引擎。由插件在每次开窗时刷新，⛔ 别手改。

⭐ 为什么要这一层：插件的安装路径**带版本号**（…/<插件>/<版本>/），升级一次就变
   ⇒ 任何写死那个路径的命令，升级后当场失效。
   而数据目录（本文件所在处）**没有版本号、升级不动** ⇒ 拿它当稳定入口。
"""
import pathlib
import runpy
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

家 = pathlib.Path(__file__).resolve().parent.parent
指针 = 家 / ".引擎路径"
if not 指针.is_file():
    print("⛔ 找不到引擎位置指针（{}）——开一个新窗即可自动重建。".format(指针))
    sys.exit(1)
目标 = pathlib.Path(指针.read_text(encoding="utf-8").strip()) / "<<引擎名>>"
if not 目标.is_file():
    print("⛔ 引擎不在了：{} ——插件可能刚升级过，开一个新窗即可重建指针。".format(目标))
    sys.exit(1)
sys.argv = [str(目标)] + sys.argv[1:]
runpy.run_path(str(目标), run_name="__main__")
'''

# ⭐ 哪些引擎需要「使用者/模型自己敲命令」调用 ⇒ 就需要一个稳定入口。
#   ⛔ 哨.py 不在其中：它只被钩子调，钩子命令里有 ${CLAUDE_PLUGIN_ROOT}，⛔ 不需要垫片。
要垫片的 = ("信.py", "全图景.py", "守望.py", "卡住哨.py")


def _铺工具(家):
    """在**稳定路径**上铺一层转发口，并把当前引擎位置写进指针。

    ⚠️⚠️ 这一层是补一个真缺口，⛔ 不是锦上添花：
      没有它，跨窗信箱的**发信那一半在插件形态下完全没有入口**——
      模型敲不出 信.py 的路径（带版本号），⇒ 收信提醒照常响、而没有人发得出信。
      ⭐ 「装了、测试也过、但真实路径上没有任何调用者」＝ 装饰。
    ⭐ 每次开窗都重写指针 ⇒ 升级后**自愈**，⛔ 不用使用者做任何事。
    """
    工具 = 家 / "工具"
    工具.mkdir(parents=True, exist_ok=True)
    (家 / ".引擎路径").write_text(str(插件根 / "engine"), encoding="utf-8")
    for 名 in 要垫片的:
        if not (插件根 / "engine" / 名).is_file():
            continue                      # 精简版没装的（如 codex派单）就不铺
        p = 工具 / 名
        # ⛔ 不用 % 格式化：模板里本来就有别的 %s，替换时会撞车
        #   （2026-08-16 实测栽过一次：抛 TypeError，被 fail-open 静默吞掉 ⇒ 工具目录一直是空的）
        文 = 垫片模板.replace("<<引擎名>>", 名)
        if not p.is_file() or p.read_text(encoding="utf-8") != 文:
            p.write_text(文, encoding="utf-8")


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
        if 事件 == "SessionStart":
            _铺工具(家)      # ⭐ 只在开窗时铺一次，⛔ 不在每次工具调用时写盘
    except Exception:
        # fail-open：⛔ 不许弄坏使用者的会话。
        # ⚠️⚠️ 但**⛔ 不许静默**——2026-08-16 实测：这里吞掉一个 TypeError，
        #   结果「铺工具」一直没成功、工具目录始终是空的，而一切看起来都正常。
        #   ⇒ 把出错原文落盘，让「没铺成」查得出来。
        try:
            import traceback
            (家 / "适配层错误.log").write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass

    # ⭐ 只在**没有显式设定**时才填，⛔ 不覆盖使用者自己的选择（也让回归测试能照常接管）
    os.environ.setdefault("WORLDBOOK_STATE_DIR", str(家 / "_state"))
    os.environ.setdefault("WORLDBOOK_ACTIVE_DIR", str(家 / "active"))
    os.environ.setdefault("WORLDBOOK_STAGING_DIR", str(家 / "staging"))
    # ⭐ 同事册：插件形态下它住在数据目录（可写、升级不丢），⛔ 不在引擎目录
    os.environ.setdefault("WORLDBOOK_关系册", str(家 / "关系册.yaml"))
    os.environ.setdefault("WORLDBOOK_工具目录", str(家 / "工具"))

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
