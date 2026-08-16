# agent-guardrails

让 AI 编码助手在**动手之前**，被你自己踩过的坑拦一下。

规矩是数据（一条一个 yaml），引擎是通用的。
⭐ 附带的 8 条只是**示范样本**，⛔ 不是推荐配置——真正管用的规矩得从**你自己的事故**里长出来。
这套东西给的是那个闭环，⛔ 不是别人的结论。

---

## 它在什么时候做什么

| 时机 | 做什么 |
|---|---|
| 工具**跑之前** | 命中规矩 ⇒ **拦一次**，要求先自查。自查完**原样重发同一调用即放行** |
| 工具跑完 | 扫工具输出／连败序列，命中 ⇒ 在模型眼前补一段提醒 |
| 一轮回复结束 | 扫最终回复，命中 ⇒ 拦一次要求自查（每会话最多 2 次，⛔ 不会缠住你） |
| 每次开窗 | 注入常驻纪律 ＋ 报告待批候选 ＋ 量上下文行数 |

⭐ 拦截的目的是**逼多想一层**，⛔ 不是禁止你做——**自查后维持原判也是合法结局**，
条目都带出口句。⛔ 没有合法出口的规矩会催生两种坏适应：为过关而扩写自证，或学会绕开触发词。

## 规矩怎么长出来

```
撞了一次事故 → 在场的模型写一条候选进 staging/
             → ⭐ 必须你点头才移进 active/ 生效
             → 每次触发记日志 → 定期盘误报率 → 误报高的收紧或删掉
```

⛔ 模型**不能**自己让一条规矩生效——这是故意的。规则库被污染比没有规则库更糟。

## 多窗联动（多个 AI 窗口之间互相通气）

同时开好几个窗做不同项目时，**「我改的东西会不会动到其他窗口/兄弟项目的模块」全靠人记**——这套东西把它机械化。

| 半边 | 它做什么 | 要不要配 |
|---|---|---|
| **收信** | 别人往你项目投了信 ⇒ 你的窗**每次工具调用后**被提醒，按 🔴急/🟡待办/🟢知会 分级 | ✅ 开箱即用 |
| **发信** | 模型自己敲一条命令，把信投进**对方项目**的收件箱 | ✅ 开箱即用 |
| **同事册** | 登记项目之间的关系 ⇒ 开窗时自动提醒「改这块要想到谁」，并直接给出发信命令 | ⚠️ **默认关着**，见下 |

⭐ **信落盘、门铃不落盘** —— 信是耐久载体，敲门只是把人叫醒。⛔ 别把内容写进敲门消息里。

**打开同事册**：编辑 `~/.claude/agent-guardrails/关系册.yaml`
（装插件时会放一份带注释的模板进去），把项目和关系填上即可。填完开个新窗就生效。

⛔ 不填也没关系——**独行侠是常态**，不填就只是不注入，⛔ 不会报错也⛔ 不会打扰你。

## 还带了什么

| 工具 | 干什么 | 怎么用 |
|---|---|---|
| `全图景.py` | 一次性扫出「这台机器上哪些项目接了这套东西、各自什么状态」 | 手动跑 |
| `卡住哨.py` | 外部程序（PS/Blender/导出器…）**弹框等人点**时，在旁边数时间、卡住了喊你 | 另开终端常驻 |
| `守望.py` | 盯住指定文件/目录，别的窗动了它就提醒 | 手动跑 |

它们都在**稳定路径**下（⛔ 不带版本号，升级不会失效）：
`~/.claude/agent-guardrails/工具/`

## 三个版本

⭐ **先看「你用的是哪个软件」，⛔ 别看名字后缀。**

| 你用的是 | 装哪个 | 它多了什么 | 状态 |
|---|---|---|---|
| **Claude Code** | `agent-guardrails` | —— | ✅ 可用 |
| **Claude Code**，且你还想**把体力活派给 codex 做** | `agent-guardrails-dispatch` | 一张**档位规划表**：哪个活用哪个模型／推理档位／沙箱权限，写死成配置，⛔ 不靠模型每次临场发挥 | ✅ 可用 |
| **codex** | `agent-guardrails`（在 codex 货架里） | 四档**子代理角色模板** ＋ 一条「起子代理前先选档」的规矩 | ✅ 可用 |

⚠️ 上面两个 Claude Code 的包**⛔ 别同时装**——它们各自完整，装两个会重复挂钩。

⚠️ **`-dispatch` 那个是 Claude Code 的包**，⛔ 不是「codex 版」。
它的意思是「**我在 Claude Code 里干活，但把体力活派出去**」。
真正跑在 codex 里的那一版在 codex 货架上，也叫 `agent-guardrails`——
⭐ 两个货架是分开的，**同名⛔ 不冲突**，你用哪个软件就装哪个货架上的。

---

## 安装（Claude Code）

### 0 · 前置：Python 3 ＋ pyyaml

```bash
python -m pip install pyyaml
```

⚠️ **这一步不能跳**。缺了 pyyaml，规矩**一条都不会加载**——插件照样装得上、钩子照样在跑、
日志照样有记录，**就是永远不叫**。（装完开窗时插件会明确告诉你它没在岗，⛔ 不会闷着。）

### 1 · 加货架、装插件

```bash
claude plugin marketplace add lmy-1219/agent-guardrails
```

```bash
claude plugin install agent-guardrails@guardrails
```

要带 codex 派单通道的，把上面第二条换成：

```bash
claude plugin install agent-guardrails-dispatch@guardrails
```

⛔ 两个别同时装——它们各自完整，装两个会重复挂钩。

### 2 · 重开一个窗

插件在**新会话**才加载。开窗后你会看到几段常驻纪律被注入——**看到了就是装上了**。

### 3 · 确认它真在岗

```bash
claude plugin list
```

看到 `Status: √ enabled` 即可。想看它自己写的心跳：

```bash
cat ~/.claude/agent-guardrails/_state/触发日志.jsonl
```

---

## 安装（codex）

⭐ 实测环境：codex CLI `0.145.0` ＋ Python 3.12 ＋ pyyaml 6。

### 0 · 前置：钩子调的是 `python` 这个名字

```powershell
python --version
python -m pip install pyyaml
```

⚠️ 只装了 `py.exe`、没有可直接运行的 `python` 命令**不够**。

### 1 · 加货架、装插件

```powershell
codex plugin marketplace add lmy-1219/agent-guardrails
```

```powershell
codex plugin add agent-guardrails@guardrails
```

⚠️ **顺序不能反**：货架还没登记就先 `upgrade` 会报 `marketplace 'guardrails' is not configured`。

### 2 · ⛔⛔ 必须单独信任钩子（这一步跳了等于没装）

开一个新的 codex 任务，输入 `/hooks`，按提示审阅并信任，然后再看一次，**四项都必须是**：

```text
SessionStart  Installed 1  Active 1
PreToolUse    Installed 1  Active 1
PostToolUse   Installed 1  Active 1
Stop          Installed 1  Active 1
```

⚠️⚠️ **`Active 0` 时动作照样执行** —— 详情页里 `[ ]` 是**关**、`[x]` 才是开。
⭐ `codex plugin list` 显示 `installed, enabled` **⛔ 不代表钩子在工作**，必须看 `Active`。

### 3 · 装四档角色（⭐ 这一步要你自己拷）

首次开窗后，四个模板在数据目录的 `角色模板/` 下（抓取／勘察／审计／实现）。
插件**⛔ 故意不自动写进你的配置目录** —— 那等于替你决定模型和文件权限。

```powershell
New-Item -ItemType Directory -Force '.codex\agents' | Out-Null
Copy-Item (Join-Path $env:USERPROFILE '.codex\plugins\data\agent-guardrails-guardrails\角色模板\*.toml') '.codex\agents'
```

⚠️ 项目级 `.codex/agents` **只在项目受信任时才加载**。拷完开一个新任务。

并行上限写在 `.codex/config.toml`：

```toml
[agents]
max_concurrent_threads_per_session = 4
```

⚠️ **两条限制，⛔ 别以为配了就万能**：
① **每个角色单独的并行数表达不了**，只有上面这个全局上限；
② 角色里的 `sandbox_mode` 是**默认值⛔ 不是绝对封锁** —— 你在父任务里临时放宽的权限会传给子代理。

### 4 · 确认它真在岗

⛔ 别只看 `plugin list`。真判据是**故意做一件该被拦的事，看它拦不拦**（照下面「自己写一条规矩」放一条只匹配测试暗号的规矩，让 codex 去创建那个文件，然后确认**文件不存在**）。

---

## 你的规矩和日志存在哪

**Claude Code**：`~/.claude/agent-guardrails/`
**codex**：`~/.codex/plugins/data/agent-guardrails-guardrails/`（codex 的数据目录卸载不删，实测过）

```
<上面那个目录>/
  active/     ← 生效中的规矩（装插件时播了 8 条示范，之后归你）
  staging/    ← 待你批准的候选
  _state/     ← 触发日志、会话计数、心跳
  工具/       ← 稳定入口（信/全图景/守望/卡住哨），⛔ 不带版本号
  角色模板/   ← codex 版专有：四档子代理角色（要你自己拷到 .codex/agents/）
  关系册.yaml ← 同事册（默认全是注释＝关着）
```

⭐⭐ **它故意⛔ 不放在插件自己的目录里**，两个原因都是实测出来的：

| 放哪 | 会怎样 |
|---|---|
| 插件安装目录 | 路径带版本号 ⇒ **升级＝换目录，写进去的全丢** |
| 插件的数据目录 | **Claude Code 的 `plugin uninstall` 会把它整个删掉** ⇒ 卸载一次，你攒的规矩和日志全没（⭐ codex 那边实测不删，所以 codex 版就用它） |

⇒ 放在上面那个位置，**升级不动它、卸载也不动它**。你攒的东西是**你的**，
⛔ 不该挂在插件的生死上。

## 升级 / 卸载

```bash
claude plugin update agent-guardrails@guardrails
```

```bash
claude plugin uninstall agent-guardrails@guardrails
```

⭐ 卸载**不会碰**你的规矩和日志（它们在 `~/.claude/agent-guardrails/`，⛔ 不在插件目录里）。
要彻底清干净，手动删那个目录即可。

---

## 自己写一条规矩

在 `active/` 放一个 yaml：

```yaml
id: 我的规矩-001
名称: 短名字
事件: PreToolUse        # 或 PostToolUse / Stop / 常驻
工具正则: '^(Bash|Write)$'
处置: 拦一次
冷却: 1
注入文本: |
  【纠偏 · 短名字】你正要……先答一句：……
  ⇒ 想过了仍维持原判 ⇒ **原样重发同一调用即放行**。
来源事故: |
  哪天、发生了什么、损失是什么。⛔ 没有真实出处的规矩多半是过拟合。
```

三条经验（都是踩出来的）：

1. **注入文本必须给出口** —— 写明「自查后维持原判怎么走」。
2. **只管 what，⛔ 不管 how** —— 管「该不该做、验没验」，⛔ 别规定解法路径。管 how 的规矩会把强模型摁进最平庸的路径，那不是纠偏，是降智。
3. **先并旧，后立新** —— 触发点的数量本身就是成本。已有相似规矩就改那条。

## 授权

MIT
