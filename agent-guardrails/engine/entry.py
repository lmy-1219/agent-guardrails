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
import io
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

# ⭐ Codex 会同时给 `PLUGIN_ROOT` 和兼容用的 `CLAUDE_PLUGIN_ROOT`。
#   ⇒ ⛔ 不能拿「没有 CLAUDE_PLUGIN_ROOT」判断宿主；实测那会让 Codex 永远误判成 Claude。
#   Claude Code 不给 `PLUGIN_ROOT`，所以它本身就是可靠的 Codex 标记。
是codex = bool(os.environ.get("PLUGIN_ROOT"))
插件根 = (Path(os.environ["PLUGIN_ROOT"]).resolve()
          if 是codex else Path(__file__).resolve().parent.parent)


def _状态家():
    """使用者的规矩与日志该落哪。

    ⛔ **不落插件目录**（安装路径带版本号，升级＝换目录 ⇒ 写进去的等于丢）。
    Claude Code：⛔ **也不落官方的每插件数据目录 `CLAUDE_PLUGIN_DATA`** ——
      ⚠️⚠️ 2026-08-16 实测：`plugin uninstall` 会把那个目录**整个删掉**
        ⇒ 使用者攒了几个月的规矩、触发日志、误报统计**一并消失**。
        而卸载常常只是为了排查问题（装回来就好），代价不该是清空资产。
    Codex：⭐ 2026-08-16 在 0.145.0 上重装、卸载后分别实测，`PLUGIN_DATA`
      和里面的中文文件都还在 ⇒ 就用宿主给的稳定目录。若宿主没给（异常环境），
      才退到 `$CODEX_HOME/agent-guardrails/`，⛔ 不写死用户名或盘符。
    ⭐ 这套东西的全部价值就是「**你自己长出来的那些规矩**」——它是使用者的资产，
      ⛔ 不该挂在插件的生命周期上。⇒ 放一个平台不会碰的固定位置。
    """
    if 是codex:
        数据 = os.environ.get("PLUGIN_DATA")
        if 数据:
            return Path(数据)
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "agent-guardrails"
    return Path.home() / ".claude" / "agent-guardrails"


def _搬旧家(新家):
    """把早期版本存在插件数据目录里的东西搬过来。⭐ 只搬一次，⛔ 不覆盖新家已有的。"""
    if 是codex:
        return                         # Codex 的新家本来就是 PLUGIN_DATA，⛔ 自己搬自己
    旧 = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not 旧:
        return
    旧 = Path(旧)
    if not (旧 / "active").is_dir() or (新家 / ".已搬家").exists():
        return
    for 子 in ("active", "staging", "_state", "工具"):
        源, 目 = 旧 / 子, 新家 / 子
        if 源.is_dir():
            目.mkdir(parents=True, exist_ok=True)
            for p in 源.rglob("*"):
                if p.is_file() and not (目 / p.relative_to(源)).exists():
                    (目 / p.relative_to(源)).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, 目 / p.relative_to(源))
    for f in ("关系册.yaml", ".已播种"):
        if (旧 / f).is_file() and not (新家 / f).exists():
            shutil.copy2(旧 / f, 新家 / f)
    (新家 / ".已搬家").write_text("从插件数据目录搬来过一次\n", encoding="utf-8")


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
import os
import runpy
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

家 = pathlib.Path(__file__).resolve().parent.parent
# ⭐ 稳定入口是使用者/模型另起一个 shell 调的，⛔ 不继承开窗 hook 进程里的环境变量。
#   ⇒ 宿主翻译也必须在这里补一次，否则 Codex 下的 信.py 会退回找 `.claude/`，
#      文件明明装着、命令也跑了，却找不到任何只带 `.codex/` 的项目。
配置目录名 = "<<配置目录名>>"
宿主用户目录 = (pathlib.Path(os.environ.get("CODEX_HOME") or (pathlib.Path.home() / ".codex"))
                if 配置目录名 == ".codex" else (pathlib.Path.home() / ".claude"))
os.environ.setdefault("WORLDBOOK_配置目录名", 配置目录名)
os.environ.setdefault("WORLDBOOK_宿主用户目录", str(宿主用户目录))
os.environ.setdefault("WORLDBOOK_STATE_DIR", str(家 / "_state"))
os.environ.setdefault("WORLDBOOK_ACTIVE_DIR", str(家 / "active"))
os.environ.setdefault("WORLDBOOK_STAGING_DIR", str(家 / "staging"))
os.environ.setdefault("WORLDBOOK_关系册", str(家 / "关系册.yaml"))
os.environ.setdefault("WORLDBOOK_工具目录", str(家 / "工具"))
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
        文 = (垫片模板.replace("<<引擎名>>", 名)
                         .replace("<<配置目录名>>", ".codex" if 是codex else ".claude"))
        if not p.is_file() or p.read_text(encoding="utf-8") != 文:
            p.write_text(文, encoding="utf-8")


def _在岗吗(家):
    """开窗自检：护栏此刻**真的在岗**吗？在岗返回 None，不在岗返回一句人话。

    ⚠️⚠️ 引擎加载条目那段是 `try: import yaml / except: return []`
      ——⇒ **一条规矩都不加载，且没有任何提示**。
      症状是「装成功了、钩子在跑、日志也有、**就是永远不叫**」，
      而这跟「我没犯过规」**长得一模一样**。⭐ 沉默不许有两种含义。

    ⭐⭐ 2026-08-16 由 codex 窗独立指出（⛔ 不是本窗自己发现的）：
      「缺 PyYAML、**规则目录不可读**、**零条合法规则** 必须**分别报明**，
        ⛔ 不能统一返回空列表。」
      ⇒ 本窗原来只堵了第一种 ⇒ 另外两种在 v0.4.0 里仍然是静默的。现在三种都报。
    """
    try:
        import yaml
    except Exception:
        return ("缺少 Python 的 pyyaml 包 ⇒ **一条规矩都没加载**。\n"
                "⇒ 装上即可恢复：`python -m pip install pyyaml`（装完重开一个窗）。")

    生效 = 家 / "active"
    try:
        文件们 = sorted(生效.glob("*.yaml"))
    except Exception as e:                       # 权限/路径坏/盘掉了
        return ("规矩目录**读不了**（%s）⇒ 一条规矩都没加载。\n"
                "⇒ 位置：%s" % (e, 生效))
    if not 文件们:
        return ("规矩目录里**一个文件都没有** ⇒ 此刻没有任何规矩在生效。\n"
                "⇒ 位置：%s（删空了？重装插件会把示范条目补回来）" % 生效)

    好 = 坏 = 0
    for p in 文件们:
        try:
            e = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(e, dict) and e.get("id") and e.get("注入文本"):
                好 += 1
            else:
                坏 += 1
        except Exception:
            坏 += 1
    if 好 == 0:
        return ("那里有 %d 个文件，但**没有一条能用**（格式不对或写坏了）\n"
                "⇒ 此刻护栏完全不工作。位置：%s" % (len(文件们), 生效))
    if 坏:
        return ("有 %d 条规矩在岗，但另有 **%d 个文件读不懂**（会被静默跳过）\n"
                "⇒ 检查一下：%s" % (好, 坏, 生效))
    return None


def 主():
    事件 = sys.argv[1] if len(sys.argv) > 1 else ""
    家 = _状态家()
    try:
        家.mkdir(parents=True, exist_ok=True)
        _搬旧家(家)          # ⭐ 早期版本把东西存在插件数据目录里，先接过来
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
    # ⭐⭐ 宿主翻译层：告诉引擎「这台宿主的项目配置在哪个目录名下、用户目录在哪」。
    #   ⇒ **一份引擎跑两个宿主**，⛔ 不复制引擎（本仓原罪就是复制）。
    #   ⛔ 这里⛔ 不写死 .claude —— 有 Codex 专属的 PLUGIN_ROOT 就认 codex 的一套。
    os.environ.setdefault("WORLDBOOK_配置目录名", ".codex" if 是codex else ".claude")
    os.environ.setdefault("WORLDBOOK_宿主用户目录",
                          str(Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
                              if 是codex else (Path.home() / ".claude")))
    # ⭐ 引擎认 CLAUDE_PROJECT_DIR 当显式锚点 ⇒ 在 codex 上把它翻译过去，⛔ 不改引擎。
    #   Codex 0.145.0 实测没有 CODEX_PROJECT_DIR / PROJECT_DIR；等价值在 hook stdin 的 `cwd`。
    #   这里读一次再原样放回去，保证后面的哨.py 仍能读到完整 hook 输入。
    if 是codex and not os.environ.get("CLAUDE_PROJECT_DIR"):
        try:
            buf = getattr(sys.stdin, "buffer", None)
            原始输入 = (buf.read().decode("utf-8", "replace")
                        if buf is not None else sys.stdin.read())
            sys.stdin = io.StringIO(原始输入)
            钩子输入 = json.loads(原始输入) if 原始输入.strip() else {}
            项目根 = 钩子输入.get("cwd") if isinstance(钩子输入, dict) else None
            os.environ["CLAUDE_PROJECT_DIR"] = str(Path(项目根 or Path.cwd()).resolve())
        except Exception:
            # hook 命令本身就在会话 cwd 下执行；stdin 坏了时退到这个已实测的等价值。
            os.environ["CLAUDE_PROJECT_DIR"] = str(Path.cwd().resolve())

    # ⭐ 开窗自检：⛔ 不在岗就**明说**，⛔ 不许悄悄空转（沉默不许有两种含义）。
    #   只在 SessionStart 说一次；⛔ 不在每次工具调用时刷屏——噪音毁信用比不提醒更糟。
    #   ⛔ 也不再往下跑引擎：这几种情况它做不了正事，跑了反而会和这条消息抢标准输出。
    if 事件 == "SessionStart":
        病 = _在岗吗(家)
        if 病:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext":
                    "⛔【agent-guardrails 没在岗】%s\n"
                    "⚠️ 在修好之前，⛔ 别把「没被拦过」当成「我没犯过规」。" % 病,
            }}, ensure_ascii=False))
            return 0

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
