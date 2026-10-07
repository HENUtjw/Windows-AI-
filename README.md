# AI 视频翻译

把 **Windows 实时字幕**识别出的英文，用 **DeepSeek** 实时翻译成中文，以**置顶悬浮窗**叠在视频上显示。

不需要 Copilot+ PC，不需要额外的语音识别模型，不占用 GPU。

```
Windows 实时字幕 ──UIA 轮询──▶ 断句器 ──▶ DeepSeek ──▶ 置顶浮窗
  (LiveCaptions.exe)          (稳定判定/去重)   (带上文)     (半透明/鼠标穿透)
```

---

## ⚠️ 先做这一步：把实时字幕的识别语言改成英语

**这是最常见的失败原因。** 如果实时字幕的识别语言停留在「简体中文」，Windows 会用
中文模型去识别英文语音，得到的是乱码，翻译自然毫无意义。

1. 按 `Win + Ctrl + L` 打开实时字幕
2. 点窗口里的 **⚙️ 设置** → **更改语言**
3. 选 **英语(美国)** → **继续**
4. 如果提示下载，点 **下载**（首次使用需要下载语音识别文件）

> **提示**：语言下拉里会列出全部受支持的语言，**设备上已下载的显示为粗体**。
> 本机检测到共 17 种可选语言，其中英文有 7 种（美国 / 英国 / 澳大利亚 / 加拿大 /
> 印度 / 爱尔兰 / 新西兰）。不确定当前设置？运行 `python tools/caption_language.py`
> 会直接把当前语言和全部可选项打印出来。

另外建议在 ⚙️ → **位置** 里选择 **覆盖在屏幕上**（官方也这么建议）。

---

## 原理：为什么能读到实时字幕

Windows 实时字幕（`C:\Windows\System32\LiveCaptions.exe`）是一个 UI Automation 提供方，
它的字幕文本可以通过标准的 Windows 辅助功能接口读取——这正是屏幕阅读器能读它的原因。

实测（Windows 11 build 26200）确认的结构：

```
[WindowControl] class='LiveCaptionsDesktopWindow'  标题='实时辅助字幕'
  └ [PaneControl] class='Windows.UI.Composition.DesktopWindowContentBridge'
      └ [PaneControl] class='Windows.UI.Input.InputSite.WindowClass'
          ├ [TextControl]    aid='CaptionsTextBlock'        ← 字幕文本在这里
          ├ [TextControl]    aid='ReadyToCaptionTextBlock'  ← 待机时才出现
          ├ [ButtonControl]  aid='SettingsButton'
          └ [ButtonControl]  aid='CloseButton'
```

关键点：**`CaptionsTextBlock` 只在字幕真正滚动时才存在**，待机时它被
`ReadyToCaptionTextBlock` 取代——两者**互斥**。采集器正是靠这个互斥关系判断状态，
并决定何时启用「AutomationId 被改名」时的启发式回退。

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | Windows 11 22H2 或更高（本机实测 build 26200 / 25H2） |
| Python | 3.8+（本机实测 3.11.7，Anaconda） |
| 依赖 | `uiautomation`、`comtypes`、`requests` —— 本机已全部安装 |
| 网络 | 能访问 `api.deepseek.com`（实测 TLS 1.3 正常） |
| 其他 | 一个 DeepSeek API Key；**英文语音识别语言包**（见开头） |

依赖若缺失：`pip install -r requirements.txt`

> ⚠️ **机器上装了多个 Python 时最容易踩坑**：依赖必须装在「你实际用来运行本项目的
> 那一个」解释器里（Anaconda 与独立安装的 Python 各自独立，互不共享）。
> 程序会检测这一点并直接告诉你该往哪个解释器装。确认当前解释器：
>
> ```bash
> python -c "import sys; print(sys.executable)"
> ```
>
> 本项目在 **Python 3.11（Anaconda）** 与 **Python 3.13（独立安装）** 上都实测通过。

> Windows 自带的实时字幕翻译功能需要 **Copilot+ PC**（Windows 11 AI+ 电脑）。
> 普通 PC 上拿不到，这正是本项目存在的意义。

---

## 快速开始

```bash
# 1. 配置 API Key —— 直接运行即可：没配 Key 会**弹出一个窗口**让你填写
#    （input 隐藏、可点「获取 API Key」跳转申请页、立即联网校验、自动写入 config.json）
#    全程不需要碰终端，也不用手动改文件。想更换随时跑 python main.py --setup
#    也可以手动编辑 config.json 的 deepseek.api_key，或设置环境变量：
#        setx DEEPSEEK_API_KEY "sk-你的Key"

# 2. 把实时字幕语言改成英语（见开头 ⚠️ 一节）

# 3. 运行
python main.py
```

播放英文视频（声音从扬声器输出），译文就会出现在屏幕底部的浮窗里。

想先看界面效果、不消耗额度：**`python main.py --demo`**

---

## 命令行参数

| 参数 | 说明 |
|---|---|
| `python main.py` | 正常使用 |
| `python main.py --demo` | 演示模式：回放内置脚本字幕 + 模拟翻译，**不需要 API Key** |
| `python main.py --setup` | 打开图形设置窗口填写 / 更换 API Key，写回 `config.json` 后验证一次 |
| `python main.py --check-api` | 验证 Key、网络与模型名（先查 `/models` 再真实翻译一次），然后退出 |
| `python main.py --console` | 不显示浮窗，只在控制台输出中英对照 |
| `python main.py --no-click-through` | 关闭鼠标穿透，可用鼠标拖动浮窗 |
| `python main.py --stable-ms 900` | 调大断句阈值：译文更完整但更慢 |
| `python main.py --position top-center` | 浮窗移到屏幕顶部 |
| `python main.py -v` | 输出调试日志 |

---

## 浮窗操作

外观仿 Windows 实时字幕：深色玻璃面板 + 圆角 + 1px 边框，右上角两个图标按钮用的是
Windows 自己的符号字体（与系统实时字幕上那两个按钮同款字形）。

| 操作 | 说明 |
|---|---|
| **拖动** | 按住面板任意空白/文字处拖动即可。位置会记住，下次启动还在原地 |
| **⚙ 设置** | 打开设置面板（见下） |
| **✕ 关闭** | 退出程序，并**一并关掉 Windows 实时字幕**（可用 `overlay.close_livecaptions` 取消这个联动） |

### 设置面板

分三页共 21 项，**改完写回 `config.json`**——不用再手改文件：

| 页 | 可调 |
|---|---|
| **翻译** | API Key（输入隐藏 / 可显示 / 换 Key 才联网校验）、模型、目标语言、并发线程、推测翻译与阈值、携带上文 |
| **外观** | 译文字号、原文字号、文字边距、同时显示几句、显示原文、不透明度、初始位置、鼠标穿透、退出联动 |
| **字幕与缓存** | 断句稳定阈值、逗号从句门槛、缓存开关与容量、短语包、命中统计、自动启动字幕、记录原始字幕 |

> **「外观」页的改动立即生效**（保存后浮窗当场变化）。翻译与断句类改动需要重启——
> 那些参数在启动时就固化了。界面上的说明文字如实写着这一点。

几个刻意的设计选择：

* **去掉系统标题栏**，自己画一条同色的。系统标题栏的颜色由 Windows 主题决定，
  浅色主题下它会在深色面板上压出一条亮白条，而且改不了。
* **卡片分组 + 拨动开关**：一组设置放进比背景略亮的卡片；开关用 pill switch
  而不是方框复选框。
* **滑块独占一行**：标题和当前值在上一行、轨道占满整行。塞在右侧会挤成一小段。
* **每项都有说明文字**，不用猜某个参数是干什么的。
* 分页条和滑块都是自绘的：`ttk.Notebook` 走系统主题（深色下是一块浅色控件），
  `tk.Scale` 在深色下会把轨道画成一串方块。
* 窗口高度**随当前页变化**：三页内容长短不同，按最高的一页固定会在矮页留下
  大片空白（实测差 200px 以上）。

> **重要变化**：浮窗的**鼠标穿透现在默认关闭**——否则按钮点不了、面板也拖不动，
> 这两个需求本身互斥。想在看视频时彻底不误触，按 `Ctrl+Alt+T` 打开穿透，
> 此时按钮会点不到（这正是穿透的定义），再按一次即可恢复。

## 热键

| 热键 | 功能 |
|---|---|
| `Ctrl + Alt + Q` | 退出程序 |
| `Ctrl + Alt + T` | 切换鼠标穿透（开着时按钮不可点，但点哪都不会误触视频） |
| `Ctrl + Alt + H` | 显示 / 隐藏浮窗 |
| `Ctrl + Alt + ↑ / ↓` | 上移 / 下移浮窗 |

---

## 配置说明（`config.json`）

完整字段见 `config.example.json`，只需写你想改的部分，其余自动用默认值。

### 翻译质量相关

| 字段 | 默认 | 说明 |
|---|---|---|
| `captions.stable_ms` | `1400` | **速度与完整性的主旋钮**。实测 1400 时平均额外等待 1.60s 且输出干净；1200 以下出现碎片；1600 以上反而涨到 2.7s、最坏 7.25s。详见「延迟与提速」 |
| `captions.clause_min_words` | `12` | **长句提速**：逗号也可作从句边界，但要求前面已有这么多词。设 `0` 关闭（退回「只在句末定稿」） |
| `translate.context_sentences` | `3` | 携带几句上文。让模型保持术语和代词一致，对长视频效果提升明显 |
| `translate.workers` | `4` | **并发翻译线程数**。DeepSeek 响应在 0.9–4.6s 间波动，单线程会越积越慢。实测每 1.5s 出句时 3 路够用、每 1.0s 出句时需 5 路；默认 4 留余量。加大不影响费用（请求数不变） |
| `translate.speculate` | `true` | **推测翻译**：定稿前先把半句送去翻译，实测命中率 86%，等于把 1.6s 断句等待藏起来。代价是请求数约 2 倍 |
| `translate.speculate_ms` | `500` | 半句的「词」稳定多久才开始推测。调小更容易命中，但白烧的推测也更多 |
| `translate.max_backlog` | `3` | 翻译跟不上说话速度时丢掉最旧的待翻译句，避免字幕越拖越远 |
| `deepseek.max_tokens` | `256` | 输出上限。字幕句很短，封顶可避免偶发长输出拖慢整句 |
| `deepseek.model` | `deepseek-flash` | 现行模型名是 `deepseek-flash` / `deepseek-v4-pro`。**旧的 `deepseek-chat` / `deepseek-reasoner` 已于 2026-07-24 完全退役**，用旧名会直接失败（程序会拦下并提示改用哪个） |
| `deepseek.thinking` | `disabled` | 新模型**默认开启思考模式**。字幕场景必须关掉——否则每句都要先推理一遍，延迟和费用会高出一个数量级 |
| `deepseek.temperature` | `1.3` | DeepSeek 官方对翻译任务的推荐值 |
| `deepseek.stream` | `true` | 流式输出，译文边生成边显示，体感延迟明显更低 |
| `deepseek.max_retries` | `2` | 限流(429)/服务端故障(5xx)的退避重试次数；401、402 属确定性错误不重试 |
| `translate.system_prompt` | 空 | 留空用内置提示词；填了就完全替换它 |
| `cache.enabled` | `true` | 第二层：AI 学到的短句缓存。短句译文获足够票数后晋升，后续相同短句（≤ `max_words` 词）跳过 API 直接复用译文 |
| `cache.max_words` | `6` | 长度闸门。超过此词数永不进缓存（防长句被语境污染） |
| `cache.min_hits` | `3` | 最高票译文的票数门槛 |
| `cache.majority` | `0.75` | 晋升还要求最高票译文**占比**达标，把语境依赖短语（最高票仅 50%）挡在缓存外 |
| `cache.min_samples` | `5` | 至少观察这么多次才开始评估晋升，防「早期走运」误判 |
| `cache.size` | `500` | canonical LRU 容量上限。约 100KB 内存，可忽略 |
| `phrasebook.enabled` | `true` | 第一层：内置高频短语包，命中即用（零延迟零成本） |
| `phrasebook.path` | 空 | 留空用 `data/phrasebook.json`；可指向自己的文件扩充 |

### 外观相关

| 字段 | 默认 | 说明 |
|---|---|---|
| `overlay.width` | `1400` | 浮窗宽度（**物理像素**，程序已声明 DPI 感知） |
| `overlay.font_size` | `21` | 译文字号（磅）。嫌大嫌小都改这里 |
| `overlay.source_font_size` | `13` | 英文原文字号 |
| `overlay.padding` | `26` | **文字到边框的距离**（四边统一）。调大更透气，调小更紧凑 |
| `overlay.show_source` | `true` | 是否显示英文原文 |
| `overlay.max_sentences` | `2` | 同时显示几句（每句含原文 + 译文两行） |
| `overlay.group_spacing` | `16` | 句与句之间的额外间距。太小会看不出哪句英文配哪句中文 |
| `overlay.placeholder_color` | `#6B7280` | 译文还没到时的「翻译中…」占位色 |
| `overlay.alpha` | `0.9` | 不透明度，`1.0` 为完全不透明 |
| `overlay.position` | `bottom-center` | 可选 `top-center` / `bottom-left` / `bottom-right`；拖动后会变成 `custom` 并记录 `x`/`y` |
| `overlay.margin_bottom` | `90` | 距屏幕底部距离（物理像素） |
| `overlay.click_through` | `false` | **鼠标穿透**。默认关闭，否则右上角按钮点不了、面板拖不动；`Ctrl+Alt+T` 可临时打开 |
| `overlay.show_buttons` | `true` | 是否显示右上角的 ⚙ / ✕ 按钮 |
| `overlay.close_livecaptions` | `true` | 点 ✕ 关闭时，是否一并关掉 Windows 实时字幕 |
| `overlay.background` | `#1C1C1C` | 面板底色（仿系统实时字幕的深色玻璃） |
| `overlay.border_color` | `#3A3A3A` | 1px 边框颜色 |
| `overlay.corner_radius` | `12` | 圆角半径。用窗口区域实现——Tk 控件只能是矩形 |

---

## 目录结构

```
AI视频翻译/
├── main.py                  程序入口（命令行参数在这里）
├── config.json              你的配置（含 API Key，已被 git 忽略）
├── config.example.json      完整配置模板
├── src/
│   ├── caption_reader.py    UIA 采集器：窗口定位、元素优先级、演示字幕源
│   ├── segmenter.py         断句与稳定判定（纯逻辑，测试完整覆盖）
│   ├── translator.py        DeepSeek 客户端（流式 / 退避重试 / 可读错误）
│   ├── overlay.py           置顶浮窗（DPI 感知、鼠标穿透、自适应高度）
│   ├── hotkeys.py           全局热键（纯 ctypes，无第三方依赖）
│   ├── app.py               主流程编排（采集/翻译/展示三条线程）
│   └── config.py            配置加载与默认值
├── tools/
│   ├── caption_language.py  查看实时字幕当前语言与全部可选语言 ← 排查首选
│   ├── capture_captions.py  抓取真实字幕（可按时间戳录制，用于调参）
│   ├── measure_latency.py   量延迟/吞吐（断句、网络、压力测试）← 调参首选
│   ├── analyze_log.py       分析录制日志：重复 / 内容丢失 / 碎片 ← 排查"漏翻/重复"首选
│   ├── preview_overlay.py   渲染浮窗 / 设置面板并截图（`--tab` 选分页）← 调外观首选
│   ├── win_capture.py       窗口抓图（上面两个工具共用）
│   ├── e2e_selftest.py      整条链路自检（--real 用真实实时字幕）
│   ├── check_uia_env.py     UIA 环境自检（区分环境问题与代码问题）
│   ├── probe_uia.py         实时字幕 UI 树探测与文本监听
│   ├── mock_deepseek.py     本地 mock 服务器（测试与自检共用）
│   └── uia_bootstrap.py     comtypes 缓存目录引导
├── tests/
│   ├── test_segmenter.py    断句器测试（63 项，含真实录制数据回放）
│   ├── test_translator.py   DeepSeek 协议层测试（49 项，本地 mock）
│   ├── test_caption_reader.py  采集器测试（32 项，假控件模拟 UI 树）
│   └── fixtures/            真机录制的实时字幕样本（回归数据）
└── logs/                    原始字幕日志（用于调参，已 git 忽略）
```

---

## 排查问题

按这个顺序跑，基本能定位所有情况：

```bash
# 1. 字幕语言对吗？（头号原因）
python tools/caption_language.py

# 2. 程序能读到字幕吗？环境有没有被限制？
python tools/check_uia_env.py
python tools/probe_uia.py

# 3. 整条链路（含浮窗、HTTP）本身好不好？
python tools/e2e_selftest.py --shot shot.png            # 脚本化字幕 + 本地替身
python tools/e2e_selftest.py --real --speak --wait 55   # 真实实时字幕 + 本地替身（自动播英文语音）
python tools/e2e_selftest.py --live --real --speak --wait 55   # 全程真实：含真实 DeepSeek（耗额度）

# 混沌注入：验证故障恢复（都是实测过的场景）
python tools/e2e_selftest.py --fail-first 3             # 前 3 次翻译请求失败后能否恢复
python tools/e2e_selftest.py --real --speak --kill-at 18 --wait 60   # 中途强杀实时字幕能否自动重连

# 延迟 / 吞吐（卡顿时先跑这个）
python tools/measure_latency.py                         # 断句延迟（回放真实数据，不花钱）
python tools/measure_latency.py --live                  # 网络延迟（真实服务）
python tools/measure_latency.py --stress --workers 1    # 压力测试：看滞后是否累积

# 分析录制日志（怀疑「漏翻」或「同一句翻了两遍」时跑这个）
python tools/analyze_log.py                             # 分析 logs/ 里最新一份
python tools/analyze_log.py --all                       # 全部分析，看趋势

# 4. API Key / 模型名 / 网络通不通？
python main.py --check-api

# 5. 断句逻辑是否正常？
python tests/test_segmenter.py
```

**跑测试：**

```bash
python tests/test_segmenter.py
python tests/test_translator.py
python tests/test_caption_reader.py
python tools/e2e_selftest.py
```

---

## 常见问题

**Q：报错 `ModuleNotFoundError: No module named 'uiautomation'`（或 comtypes）**

你用的那个 Python 里没装依赖。**装了多个 Python 时最容易踩这个**——Anaconda 和独立
安装的 Python 各自独立，装在一个里另一个用不了。

程序现在会直接告诉你该往哪个解释器装，照着执行即可：

```
  请**用同一个解释器**安装依赖（这一点最关键）：

        "D:\Python\python.exe" -m pip install -r "...\requirements.txt"
```

引号里必须是**你实际用来运行本项目的那一个**。用这条命令确认：

```bash
python -c "import sys; print(sys.executable)"
```

**Q：浮窗出来了，但一直显示「等待实时字幕…」**

按概率排序：
1. **实时字幕的识别语言不是英语。** 跑 `python tools/caption_language.py` 确认。
2. 实时字幕窗口没开（`Win + Ctrl + L`）。
3. 视频没声音，或声音走了耳机独占模式。实时字幕只处理**默认输出设备**上的声音，
   `设置 → 系统 → 声音` 里的默认设备必须是对的。
4. 实时字幕还停在首次运行的隐私同意界面，没点「继续」。

**Q：译文出来但明显是乱码/风马牛不相及**

识别语言错了。英文视频必须用英文模型识别，中文模型会把英文听成中文谐音。

**Q：翻译有点慢 / 句子被截断**

调大 `captions.stable_ms`、`translate.context_sentences`，并确认 `deepseek.stream` 为 `true`。

**Q：报错 `HTTP 401` / `HTTP 402` / `HTTP 429`**

- `401` = API Key 无效
- `402` = 余额不足
- `429` = 被限流，程序会自动退避重试；频繁出现就调大 `captions.stable_ms` 降低请求频率

这些提示会直接显示在浮窗里，不会静默失败。

**Q：浮窗挡住了视频，或者文字发虚**

字号/宽度/透明度和位置都在 `config.json` 的 `overlay` 段里调。程序已声明 DPI 感知，
文字不会因为 150% 缩放而发虚。

**Q：怎么知道 API 花了多少钱？**

DeepSeek 控制台可见。字幕场景每句都很短，配合 `context_sentences=3` 消耗很低。
调大 `captions.stable_ms` 能减少请求次数。

**Q：日志文件在哪？**

`logs/captions-*.log`，记录每一次原始字幕文本，用来判断 `stable_ms` 是否合适。

**Q：怎么关掉短句缓存？**

A：设置面板里取消勾选，或 `config.json` 改 `cache.enabled=false`（热更新，无需重启）。关掉
后若发现某短句译得不对，请确认是不是缓存误命中。

---

## 真实数据实测发现（重要）

用 `tools/capture_captions.py` 抓下真实字幕后的结论，它们直接决定了程序的行为：

1. **`CaptionsTextBlock.Name` 返回整场会话累积的完整转录**，不是窗口上可见的那两行
   （实测一次 35 秒的讲话就累积了 6 行且仍在增长）。所以程序启动时会先跳过已有
   历史——否则中途启动会把几百句旧内容全部送去翻译——并且每轮只处理「新变成
   非最后一行」的那些行。
2. **实时字幕会给「还没说完的半句」也补句号**：::

       Econom. → Economics is. → Economics is not. → Economics is not just.
       → … → Economics is not just about money.

   也就是说**句末标点不代表句子结束**。判据改为「这一句后面还有没有文本」：
   有后续文本才说明它确实说完了。
3. **真句尾到补上标点之间固定间隔约 1.61s**；词级更新中位间隔 0.31s，实测最大 2.34s。
4. **约一半更新是「改写」而非「追加」**：识别器会回溯插入逗号/句号、调整大小写，
   甚至丢掉连字符（`trade-off` → `trade off`）。
5. **同一段语音会被重新识别成不同版本**（`Let us begin with…` 与 `Begin with…`），
   只有「包含关系」判断才能去重，前缀判断抓不到。
6. **数字表示也会被回溯改写**：`one of` ↔ `1 of`。这一条曾经造成真实的 bug ——
   程序已经在 `...from one of the most famous economists of all times,` 处定稿翻译过，
   改写后同一句变成 `...from 1 of...`，逐字比较认不出是同一句，于是**隔 11 秒又翻译
   了一遍**（用户报的「有一些话会重复翻译」）。而且重复的那句内容来自更早、却排在
   更新的句子后面，所以同时表现为「翻译顺序有问题」。
   修法：去重比较改用归一化签名（去标点 + 小写 + **数字统一成英文单词**）。
7. **转录是一个滚动窗口，会翻页**：实测讲了一段之后稳定占满 **12 行**，
   之后每来一行就顶掉最旧的一行（一次 16 分钟的运行里翻了 **34 次**）。
   这一条曾经造成**严重的内容丢失**：窗口满了以后 `_processed_lines` 早已顶到上限，
   被顶到倒数第二的那一行**再也不会被定稿**，它未提交的残句随 `_pending` 被新尾行
   覆盖而静默消失。实测 44 行里有 **6 行（14%）从头到尾一个字都没被翻译过**
   （例如 `Air, for most of human history, has been considered a free resource.`）。
   修法：检测翻页（用归一化签名判断旧尾行是否「上移」），翻页前先把残句定稿；
   残留若只是单个功能词（`Of.` / `And.`）则判为识别噪声丢弃。
   修复后同一份日志：提交句数 **193 → 230**，未覆盖行 **14% → 5%**（且剩余两行
   经细查其实已覆盖，只差开头的识别噪声）。
8. **去重必须在「词」的层面比较，不能在字符串上做字符比较**。这一条造成过
   隐蔽而严重的丢失：旧写法是 `a.startswith(b)`，于是候选
   `all of the debian packages…` 会被上一句 `A.` 判成重复
   （`"all".startswith("a")` 为真）而**整句丢弃**。
   `A.` `So.` `I.` 这类碎片一出现，后面同首字母的长句就会被吞掉。
   实测（26 分钟日志）它让 `All of the Debian packages I installed on Ubuntu…`
   一个字都没被翻译。改成 token 级比较后，同一份日志的**真丢失降到 0**。
9. **数字写法的回溯改写有多种形态**：`one of` ↔ `1 of`、
   `July Nineteenth at Nine AM` ↔ `July 19th at 9:00 AM`。
   归一化要同时处理基数词、序数词（`nineteenth` ↔ `19th`）和时钟（`9:00` → `9`）。
   尚未覆盖 `seventeen seventy six` ↔ `1776` 这种多位拼读——
   它需要把多个词合并成一个数字，而那会破坏「按词数切出新增部分」的对齐，
   宁可少认一种。

---

## 已实测验证的部分

| 验证项 | 结果 |
|---|---|
| 实时字幕 UI 结构 | 已 dump 真实窗口树，确认 `CaptionsTextBlock` / `ReadyToCaptionTextBlock` 互斥 |
| 实时字幕可用语言 | 已枚举：17 种，其中 7 种英语；本机英文语音包**已安装**，无需下载 |
| 采集器连接真实窗口 | 通过（hwnd 定位 → 元素优先级 → 状态机 → 工作线程 COM 初始化） |
| 真实字幕文本格式 | 已实测录制并作为回归数据（`tests/fixtures/`） |
| 断句/去重逻辑 | 63 项单元测试通过（含真实数据回放、半句带句号、重识别去重、启动跳过历史） |
| DeepSeek 协议层 | 50 项测试通过（本地 mock：SSE 流式、上文携带、401/402/429/5xx、退避重试、限流后恢复、模型名与思考开关） |
| 采集器元素优先级 | 32 项测试通过（假控件模拟真实 UI 树） |
| 端到端（脚本化字幕 + 本地替身） | 15 项断言通过：真实 app + 真实浮窗 + 真实 HTTP |
| **端到端（真实实时字幕 + 真实 DeepSeek）** | **15 项断言全部通过**：真实 Windows 实时字幕英文文本 → 断句 → **真实 DeepSeek 翻译** → 浮窗中文显示 |
| **混沌：实时字幕中途被强杀** | **18 项断言通过**：程序不崩 → 自动重连 → 继续产出译文 |
| **混沌：翻译请求连续失败** | **19 项断言通过**：浮窗显示 ⚠ 提示 → 之后自动恢复正常翻译 |
| 延迟 | 断句 1.60s（原 2.76s）+ 网络首字 0.96s；**推测翻译命中率 86%**，命中时该段等待被完全隐藏 |
| 吞吐 | 每 1.5s 出句时，滞后从前 3 句 1.47s 稳定在最后 3 句 1.10s（单线程会累积到 4.09s） |
| 浮窗显示 | 6 种状态（正常/流式/占位/超长/等待/报错）逐张渲染核对过，可用 `tools/preview_overlay.py --all` 复查 |
| 浮窗交互 | 设置面板可正常构建/销毁、三页都能切换；保存能按点号路径写回配置且保留其它项；改字号/句数后浮窗**当场变化**且已显示内容不丢 |
| 首次配置 | 用无 Key 的配置启动 `main.py`，实测弹出「首次配置」窗口、**控制台无任何终端提问**；取消后不再回退到终端追问（12 项路由测试） |
| 顺序与重复 | 用**真实运行日志回放**验证：修复前同一句被提交两次（`one of` / `1 of`），修复后 8 句降为 7 句、重复消失；翻译上文按提交顺序排列（并发乱序完成也不受影响） |
| 内容不丢失 | 用 16 分钟 + 26 分钟两份真实日志回放验证：翻页修复后提交句数 **193 → 230**，未被翻译的行 **14% → 5%**；再去重改到词级后**真丢失 0**（26 分钟 / 103 行 / 3091 条更新） |
| 浮窗渲染 | `PrintWindow` 抓取与屏幕抓取结果一致，样式（穿透/不抢焦点/置顶）全部生效 |
| 配置健壮性 | 带 UTF-8 BOM 的 `config.json` 也能正常读取（PowerShell 与部分编辑器的保存格式） |
| 优雅退出 | 控制台与浮窗两种模式收到中断信号均以退出码 0 收尾 |
| **DeepSeek 真实服务** | **已实测通过**：`GET /models` 返回账号可用模型 `deepseek-flash` / `deepseek-v4-pro`；真实翻译往返 **0.70s**，译文为自然中文 |
| 短句缓存 | `tests/test_cache.py` 8 项单测通过 + `tools/e2e_selftest.py` 命中/未命中断言通过；同一短句投送 5 次后 `cache.size ≥ 1`、第 4 次起命中 |

---

## 延迟与提速

「说完话 → 看见译文」由两段构成，都可以用 `tools/measure_latency.py` 量出来：

| 环节 | 实测 | 说明 |
|---|---|---|
| **断句延迟** | 平均 **1.60s**，最坏 1.96s | 本地等待，由 `captions.stable_ms` 控制 |
| 网络延迟 | 首字 **0.96s** / 全程 0.97s | 流式输出让两者几乎相等，感知延迟 ≈ 首字延迟 |

### 关键：网络延迟会大幅波动，所以必须并发

实测**同样的请求、同样的上下文**，DeepSeek 的响应在 **0.9s 到 4.6s 之间波动**
（两轮之间就能差 2.5 倍）。上下文长短几乎不影响（192 vs 250 输入 token）。

所以真正的风险不是「平均慢」，而是**一旦 API 变慢、视频又出句快，单线程串行
的队列就会越积越长，字幕越看越不同步**。实测对比（每 1.5 秒出一句）：

| | 前 3 句滞后 | 最后 3 句滞后 | 结论 |
|---|---|---|---|
| 单线程 | 1.21s | **4.09s** | 持续累积 ✗ |
| **4 路并发（默认）** | 1.47s | **1.10s** | 保持平稳 ✓ |

`translate.workers` 就是并发线程数（默认 4）。并发对质量的影响很小：每个线程
取「上文」时拿的是当时已完成的最新几句，顺序可能略有出入，而上文只用于提升
一致性，不影响正确性。复测：

```bash
python tools/measure_latency.py --stress --workers 1   # 看串行如何累积
python tools/measure_latency.py --stress --workers 3   # 看并发如何平稳
python tools/measure_latency.py --stress --interval 1.0 # 更快的语速
```

### 三层翻译链路（越靠前越快）

```
内置短语包  →  AI 学到的短句缓存  →  DeepSeek
（零延迟）      （零延迟）            （约 0.9s）
```

**第一层：内置短语包**（`data/phrasebook.json`，111 条）

命中即用，不调 API、不占并发名额。质量控制靠**收录原则**而不是事后校验：只收
「脱离上下文也只有一种正确译法」的短语（问候、致谢、回应、话语标记）。
凡意思随语境变化的（`Fine.` 可能是「好的」也可能是「罚款」）一律不收，交给 AI。

匹配要求**整句相等**，所以 `Yeah.` 命中，而 `Yeah, I think so.` 整句交给 AI——
短语包不会把某个词的译文塞进更长的句子里。

> **想更快**：把你视频里反复出现的固定说法加进这个文件即可，这是提高命中率
> 最直接的办法（比如讲座里的术语）。键按「小写、去标点、空格归一」写。

**第二层：AI 学到的短句缓存**

同一短句（≤6 词）的译文获得足够票数后晋升，之后直接复用。

> ⚠️ **这一层以前几乎从不命中。** 原因是旧规则要求「同译文**连续** `min_hits`
> 次」，而实测 `temperature=1.3` 下同一短句会有多种译法：
>
> | 短语 | 译文分布 |
> |---|---|
> | `Yeah.` | 嗯。×6（完全稳定） |
> | `Right.` | 对。×3 / 好的。×2 / 嗯。×1 |
> | `So, let us begin.` | 6 次 5 种译法 |
>
> `Right.` 连续 3 次相同的概率只有百分之十几，`So, let us begin.` 则永远不可能。
> 改成**多数票 + 占比门槛**后：完全稳定的短语照常晋升，而 `Right.`（最高票仅
> 50%）被正确挡下——**确定性短语进缓存，歧义短语不进**。
>
> `cache.min_samples`（默认 5）是第二道保险：少了它，`Right.` 会在第 4 次观察
> 时恰好凑成 3/4 = 75% 而被误判为「稳定」。

### 长句提速：逗号从句（默认开启）

一个 40 词的句子如果只认句末标点，就要**等整句说完**才定稿——实测等待可超过
8 秒，而且「末尾词稳定」的推测翻译在连续朗读时**永远触发不了**（每 300ms 就变一次）。

所以逗号也被当作从句边界，让长句边说边出：**但要求逗号前面至少有 12 个词**
（`captions.clause_min_words`），否则 `However,` / 列表枚举会被切成一地碎片。

真实录制数据实测：`when you watch this video,` 这一从句在 **20.45s** 就出现，
而整句要等到 **23.45s** —— 早了 3 秒；句子更长时差距更大。

> 顺带修掉一个**内容丢失**的 bug：实时字幕的句号迟到时，逗号从句会把两句话拼在
> 一起（如 `...opportunity cost When you watch this video,`）。旧逻辑判定为
> 「前缀重复」后**整句丢弃**，后半句从此消失。现在只翻译新增部分。

### 推测翻译：把断句等待藏起来（默认开启）

断句必须等实时字幕把句子说完（约 1.6s），这段等待是延迟的主要来源。程序的做法是
**在句子正式定稿之前，就先把「词已经稳定」的半句送去翻译**——定稿时那句往往只是
补了个句号，于是译文已经就绪，这段等待等于不存在。

离线模拟与真机实测一致：**命中率 86%**（7 句里 6 句译文提前就绪）。代价是请求数
约增加 2 倍；按 `deepseek-flash` 的价格算，一小时视频大约多花几分钱，可以忽略。

细节：定稿时如果推测**还在跑**就直接等它（它比现在才发请求更早完成）；如果它**还
排在队列里没开始**，就丢弃并走正常翻译——否则等待可能反而更久。

想关掉：`translate.speculate` 设为 `false`。

### 断句阈值：调大反而更慢

| 取值 | 平均额外等待 | 最坏 | 输出质量 |
|---|---|---|---|
| 900 / 1200 | 1.19 / 1.45s | 1.96s | 出现 `That trade.` 这类碎片 |
| **1400（默认）** | **1.60s** | **1.96s** | 干净 |
| 1600 / 1800 | 2.73 / 2.76s | **7.25s** | 干净但慢 |

**阈值调大反而更慢**，这是最反直觉的一点：实时字幕在句尾之后约 1.61s 才补标点，
阈值正好卡在这个边界上时，程序会一直等下一句接上来才敢定稿，运气不好就掉进
7 秒多的等待。1400ms 既在边界之下（不必等标点），又高于实测最长句内停顿（1.31s）。

复测方法：

```bash
python tools/measure_latency.py                      # 断句延迟（回放真实数据，不花钱）
python tools/measure_latency.py --live               # 网络延迟（真实服务，耗额度）
python tools/measure_latency.py --stable-ms 1200     # 试别的阈值
```

另外两项提速措施：

* `deepseek.max_tokens`（默认 256）：给输出封顶，避免偶发长输出拖慢整句
* `translate.max_backlog`（默认 3）：翻译跟不上说话速度时（网络慢、对话密集），
  丢掉最旧的待翻译句，保证字幕贴近「当前时刻」而不是越拖越远

### 短句缓存（默认开启）

对话密集时大量句子是重复短语（"Yeah," / "Right?" / "Let's see," 等），这些走一次 API 就够。
缓存对每个签名做「连续相同译文 ≥ 3 次才晋升」——这样语境依赖型短语（"Right." 表同意 vs
"Turn right."）会因译文不一致而**自然不会**晋升。命中后跳过 API、不消耗并发名额。

- 仅 ≤ 6 词的短句有资格进缓存；
- 关闭后立即清空内存，避免脏命中（设置面板 / `config.json` 改 `cache.enabled=false`）；
- 命中时浮窗直接显示译文，跳过「翻译中…」占位。

实测：快语速对话里约 10–25% 的句子完全省掉一次 API 调用。

---

## 已知限制

* 只读取**实时字幕已经识别出来的文本**。识别准确率取决于 Windows 的语音模型，
  程序只负责翻译，不改善识别。
* 依赖 `LiveCaptionsDesktopWindow` 这个窗口类名和 `CaptionsTextBlock` 这个 AutomationId。
  系统大版本更新可能改名——已做启发式回退（并通过测试锁定优先级：待机时绝不把
  「已准备好…」这类界面提示当成字幕），但仍建议用 `tools/probe_uia.py` 复核。
* 程序**不会**自动修改实时字幕的语言设置，需要你手动改成英语（会触发语言包下载）。
* 浮窗不拦截鼠标（设计如此）。要拖动就按 `Ctrl+Alt+T` 关掉穿透。
* **断句由 Windows 的语音模型决定**，程序不做跨句拼接。实测识别器偶尔会把一句话
  拆成两段（例如把 `Let us.` 单独成句），此时会多出一条很短的译文——这是识别结果
  使然。反过来，短句（`Thank you.` / `Of course.`）本身是合法的，所以不适合按长度
  过滤。
* 翻译有约 1 秒量级的固有延迟（断句稳定阈值 + 网络往返），不适合对同步要求极高的场景。
