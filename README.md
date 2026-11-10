# astrbot_plugin_mimotts

<div align="center">

**让 AstrBot 真正「开口说话」——小米 MiMo TTS 全自动语音条插件**

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.13.0-orange.svg)](https://github.com/AstrBotDevs/AstrBot)
[![MiMo](https://img.shields.io/badge/MiMo--V2.5--TTS-Xiaomi-red.svg)](https://platform.xiaomimimo.com/)
[![Platform](https://img.shields.io/badge/platform-QQ%20%7C%20WeChat-green.svg)](#-平台支持矩阵)

一键安装 · 零代码接入 · 无需调用工具 · 自动情绪演绎

</div>

---

## 📖 这是什么

`astrbot_plugin_mimotts` 把小米 **MiMo-V2.5-TTS** 系列的语音合成能力接到 AstrBot 上，并且**不需要模型主动调用任何工具**——LLM 的每条最终回复会按你设定的概率自动变成语音条，附带自然的情绪与语气。

它同时支持三条路线：**内置音色**（8 款开箱即用）、**VoiceDesign**（用一句话描述捏出新音色）、**VoiceClone**（用一段干音复刻音色）。

> **关于费用**：MiMo TTS 目前为小米官方的**限时免费**服务。本插件本身永久开源免费（MIT），但语音合成用量与计费政策由小米 MiMo 开放平台决定，**请以[官方开放平台](https://platform.xiaomimimo.com/)公告为准**。本插件不收取任何费用，也不代收任何费用。

---

## ✨ 核心特性

| | 特性 | 说明 |
|---|---|---|
| 🎙️ | **全自动发声** | `on_decorating_result` 钩子拦截 LLM 最终回复，自动合成并拼接语音，**无需模型调用工具**，无需改人设 |
| 🎭 | **导演手记** | 支持 `(温柔 撒娇)` 风格前缀与 `[轻笑]`、`[叹气]`、`[低语]` 等动作标签，情绪可以被「导演」 |
| 🎨 | **三种音色模式** | 8 款内置音色 / VoiceDesign 文字捏音色 / VoiceClone 音频克隆，一个插件全覆盖 |
| 📱 | **双端原生语音条** | QQ（NapCat / OneBot v11 / 官方机器人）直发 RECORD 语音条；个人微信自动降级为音频文件，**绝不丢消息** |
| 🎲 | **概率 + 字数闸门** | 不是每条都念。可设触发概率、最小/最大字数，避免刷屏与超长播报 |
| 🧹 | **Markdown 自动清理** | 代码块、链接、加粗、表格等会被剥离，**绝不会把代码念出来** |
| 🔑 | **免二次配置** | 自动复用 AstrBot 原生 MiMo TTS provider 的 `api_key` 与 `api_base` |
| 🛡️ | **防双重扣费** | 检测到原生 TTS 仍启用时自动让位，避免同一句话合成两次 |
| 🚫 | **三重去重** | 流式跳过 / 结果链已有语音段跳过 / 本轮工具已发声跳过，不重复念 |
| 🧩 | **协议严格对齐** | 遵循官方 `messages` 协议；`<style>…</style>` 自动转换（Issue #7957 修复点），**标签绝不泄漏进正文** |

---

## 🛠️ 快速安装

### 方式一：插件市场（推荐）

1. 打开 AstrBot WebUI → **插件管理** → **插件市场**
2. 搜索 `mimotts` 或 `MiMo 语音合成`
3. 点击安装，然后在插件列表中**启用**
4. 按 [⚙️ 配置说明](#-配置说明) 填好 API Key（或直接复用原生 provider 的 Key）

### 方式二：手动 Clone

```bash
cd /你的/AstrBot/data/plugins
git clone https://github.com/Zyw052/astrbot_plugin_mimotts.git
```

重启或重载插件后，在 WebUI 插件管理中启用。

### 依赖（可选但推荐）

| 依赖 | 是否必需 | 作用 |
|---|---|---|
| `ffmpeg` | 推荐 | 统一 24kHz / 单声道、生成 mp3。缺失时直接使用 MiMo 原始 WAV（已是 24kHz/mono），功能不受影响 |
| `pysilk` | 可选 | 自行编码 Tencent SILK，QQ 语音兼容性更好 |
| `aiohttp` | 无需安装 | AstrBot 自带 |

```bash
# Debian / Ubuntu
sudo apt install -y ffmpeg

# 可选：SILK 编码
pip install pysilk-mod
```

### 安装后 30 秒自检

在任意会话发送 `/mimo状态`，应能看到类似输出：

```
· 模型：mimo-v2.5-tts（音色：冰糖）
· Key 来源：原生 TTS provider（自动复用）
· 原生 TTS：未启用（provider=-）
· 自动发声：开启（概率 0.60，模式 voice_with_text，字数 2~200）
· 平台：aiocqhttp
· ffmpeg：可用
```

看到 Key 已解析、自动发声为「开启」，就可以直接聊天了。

---

## ⚙️ 配置说明

插件共 **33** 项配置，以下是最常调整的关键项。完整说明见 `_conf_schema.json` 或 WebUI 中的 hints。

### 核心项

| 配置 | 类型 | 默认 | 推荐值 | 说明 |
|---|---|---|---|---|
| `enable` | bool | `true` | `true` | 插件总开关（指令 + 工具 + 自动钩子） |
| `enable_auto_hook` | bool | `true` | `true` | 全局自动发声。关闭后只用 `/mimo` 与 `mimo_speak` |
| `auto_speak_probability` | float | `0.6` | `0.4 ~ 0.7` | 触发概率。`1.0`=每条都发声，`0.0`=从不。**调低可显著减少刷屏** |
| `send_mode` | string | `voice_with_text` | 见下 | `voice_with_text`=文字+语音条；`voice_only`=只发语音（保留图片/引用） |
| `default_voice` | string | `冰糖` | 按喜好 | 内置音色：冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean |
| `default_style` | string | `温柔 撒娇` | 按喜好 | 兜底风格，会话风格与文本内风格优先；**留空表示不加风格** |
| `min_text_len` | int | `2` | `2 ~ 3` | 有效字数下限，过滤「嗯」「好的」这类短回复 |
| `max_text_len` | int | `200` | `100 ~ 200` | 有效字数上限，**超过直接跳过（不截断）**，避免长播报 |
| `enable_prompt_injection` | bool | `true` | `true` | 幂等注入导演提示词，教 LLM 使用标签 |
| `auto_hook_skip_if_native_tts` | bool | `true` | **`true`** | 原生 TTS 启用时让位。**强烈建议保持开启**，否则会双重扣费 |

### 平台白名单

| 配置 | 默认 | 说明 |
|---|---|---|
| `target_platforms` | `aiocqhttp` / `weixin_oc` / `qq_official` | 只在这些适配器上自动发声；**留空 = 全部平台** |
| `weixin_voice_mode` | `auto` | `auto`=先试原生语音条，失败降级 mp3；`native_voice`=只发语音条（失败报错）；`file`=直接发文件 |
| `qq_record_format` | `auto` | `auto`=有 pysilk 就编码 SILK，否则发 WAV 由 NapCat 转码 |

### 凭据

**Key 解析优先级**（上者优先，命中即止）：

1. AstrBot 全局配置 `cmd_config.json` 中原生 MiMo TTS provider 的 `api_key` ✅ **推荐，零配置**
2. 插件配置 `api_key`
3. 环境变量 `MIMO_API_KEY`
4. 凭证文件 `credentials_path`（默认 `/data/apps/credentials/mimo.env`，支持 `.env` 格式或单行裸 Key）

> 已配过 AstrBot 原生 MiMo TTS 的用户**无需再填任何 Key**。

**端点优先级**：插件 `api_url`（非默认值）→ 原生 provider 的 `api_base` → 内置默认端点。

**获取 API Key**：前往 [小米 MiMo 开放平台](https://platform.xiaomimimo.com/) 注册并创建密钥（格式 `sk-xxxxx`）。

---

## 🎭 进阶玩法

### 一、玩转导演手记

MiMo 支持两种标签，**风格前缀**控制整体气质，**动作标签**控制局部细节。

#### 1. 风格前缀（必须位于正文最开头）

| 写法 | 效果 |
|---|---|
| `(温柔 撒娇)你好呀` | 官方语法，圆括号 |
| `（慵懒）让我再睡五分钟` | 全角括号同样支持 |
| `<style>清冷</style>别说话` | 自动转换为 `(清冷)别说话` |
| `(唱歌)原谅我这一生不羁放纵爱自由` | 唱歌风格 |

> ⚠️ **风格标签必须在正文最开头**。放在中间或结尾不会生效。
>
> 💡 `<style>` 写法是本插件为兼容旧习惯提供的语法糖，**绝不会原样进入朗读正文**（这是 Issue #7957 的修复点）。

#### 2. 动作标签（可插在任意停顿处）

| 类别 | 常用标签 |
|---|---|
| 笑 | `[轻笑]` `[大笑]` `[笑]` |
| 呼吸 | `[叹气]` `[长叹]` `[吸气]` `[深呼吸]` `[打哈欠]` |
| 情绪 | `[撒娇]` `[娇嗔]` `[哽咽]` `[抽泣]` `[哭]` `[颤抖]` |
| 音量 | `[小声]` `[喃喃]` `[低语]` `[耳语]` `[停顿]` |
| 其它 | `[咳嗽]` |
| 英文兼容 | `[whisper]` `[laugh]` `[sigh]` |

**实战示例**：

```
/mimo (温柔 撒娇)主人今天辛苦啦[轻笑]
/mimo (慵懒)让我再睡五分钟嘛[叹气]
/mimo (清冷)[低语]别说话，听我说
```

> 关闭 `enable_audio_tags` 后，动作标签会在合成前被剥离（正文照常朗读）。

#### 3. 让 LLM 自己会用标签

`enable_prompt_injection: true`（默认）时，插件会**幂等**地向 system prompt 追加一段导演指导词，教 LLM 在回复中使用风格前缀与动作标签。特性：

- **幂等**：已有 `[MiMo-TTS]` 标记则不再重复注入；
- **不破坏人设**：只追加到末尾，绝不替换原有内容；
- **可自定义**：填 `prompt_injection_text` 覆盖内置文案。

### 二、配置 VoiceClone（音色克隆）

VoiceClone 用一段参考干音复刻音色。**样本质量直接决定克隆效果**。

#### 1. 准备干音

| 要求 | 说明 |
|---|---|
| **时长** | **5 ~ 10 秒**（太短特征不足，太长无收益且增大体积） |
| **内容** | 连续自然地说话，避免长静音、避免音乐/音效 |
| **信噪比** | **无背景噪音**、无混响、无他人声音 |
| **格式** | `mp3` 或 `wav` |
| **规格化** | 建议转为 **24kHz / 单声道**，与 MiMo 输出规格一致 |
| **体积** | base64 编码后**必须 < 10MB**（超限 MiMo 会拒绝，插件会提前报错拦截） |

用 ffmpeg 规范化（推荐）：

```bash
# 截取 5~10 秒、转 24kHz 单声道、轻度降噪后的样本
ffmpeg -i 原始音轨.wav -ss 0 -t 8 -ac 1 -ar 24000 -c:a libmp3lame -q:a 4 voice_sample.mp3
```

#### 2. 配置

```yaml
model: mimo-v2.5-tts-voiceclone
voice_sample_path: /绝对路径/voice_sample.mp3   # 必须是绝对路径
voice_sample_format: mp3                        # 无扩展名时用于推断 MIME
```

#### 3. 注意

- **本仓库不附带任何音频样本**（版权与体积原因）。`plugin_data` 目录下的音频属于本地运行期数据，不会随插件分发，请**自备有权使用的干音**。
- 请确保你对所用干音拥有合法使用权，不要克隆他人声音用于冒充、欺诈等用途。
- 样本换成真人录音后，克隆效果通常明显优于合成样本。

### 三、VoiceDesign（文字捏音色）

不用录音，直接描述你想要的音色：

```yaml
model: mimo-v2.5-tts-voicedesign
voice_design_prompt: "温婉的年轻女声，音色清亮柔和，语气自然亲切，语速偏慢，带一点撒娇的尾音。"
```

建议 1~4 句，描述**性别、年龄感、音色质感、语气、语速**。开启 `voice_design_optimize_text` 可让官方服务端优化描述文本。

---

## 🧩 工作原理

```
用户消息
   │
   ├─[on_llm_request]──► 幂等注入导演提示词（教 LLM 用 (风格) 与 [轻笑]）
   │
LLM 生成回复
   │
   └─[on_decorating_result]──► 自动发声闸门（11 道）
         ① 插件启用 / 自动发声开启
         ② 平台在白名单内
         ③ 结果是 LLM 最终回复（指令输出不处理）
         ④ 非流式输出
         ⑤ 本轮未由 mimo_speak 发声
         ⑥ 结果链尚无语音段
         ⑦ 清理 Markdown 后仍有正文
         ⑧ min_text_len ≤ 有效字数 ≤ max_text_len
         ⑨ 概率命中 auto_speak_probability
         ⑩ 原生 TTS 冲突检查（防双重扣费）
         ⑪ 能解析到 API Key
              ↓ 全部通过
         合成 WAV → ffmpeg 统一规格 → pysilk/mp3
              ↓
     send_mode = voice_with_text → 文字 + 语音条
     send_mode = voice_only      → 仅语音条（保留图片/引用）
```

**任何一步不通过都保持原有纯文本回复**。钩子异常会被捕获并记录，**绝不影响正常聊天**。

### 有效字数怎么算

只统计「会发音」的字符：忽略空白与 `[动作标签]`。

- `[轻笑]好的` → 有效 **2** 字
- `嗯` → 有效 **1** 字（默认 `min_text_len=2` 会跳过）

### 为什么不会重复发声

| 场景 | 处理 |
|---|---|
| 模型调用 `mimo_speak` 后又输出总结 | 工具成功即打事件标记，钩子跳过 |
| 其它插件 / 原生 TTS 已加语音段 | 结果链检测到 `Record` 即跳过 |
| 流式输出 | 主动跳过（结果链尚未定稿） |
| `/mimo` 等指令输出 | 非 LLM 结果，钩子不处理 |

---

## 📱 平台支持矩阵

| 平台 | 适配器 ID | 语音条 | 说明 |
|---|---|---|---|
| NapCat / OneBot v11 | `aiocqhttp` | ✅ **原生语音条** | 发送 `Record` 段，框架转 base64 交给 NapCat 转 silk |
| QQ 官方机器人 | `qq_official` | ✅ **原生语音条** | 直接发 `Record`，由框架转 tencent_silk |
| 个人微信 | `weixin_oc` | ⚠️ **降级为 mp3 文件** | AstrBot 出站仅实现 Image/Video/File；插件先试私有通道（实验性），失败自动降级，**不丢消息** |

音频处理链路：

```
MiMo WAV (24kHz/mono) → ffmpeg 统一 pcm_s16le/24k/mono ─┬→ [pysilk] → .silk  (QQ)
                                                          └→ libmp3lame → .mp3  (微信降级)
```

---

## 🕹️ 指令一览

| 指令 | 别名 | 说明 |
|---|---|---|
| `/mimo <文本>` | `mimo语音`、`mimo说话` | 合成并发送语音条 |
| `/mimo引用` | `mimo读引用`、`mimo_quote` | 朗读被引用的消息（先引用一条文本消息） |
| `/mimo音色 [名称]` | `mimo_voice` | 查看 / 切换本会话音色（`重置` 恢复默认） |
| `/mimo风格 [风格词]` | `mimo_style` | 查看 / 设置本会话默认风格（支持多词） |
| `/mimo状态` | `mimo_status` | 自动发声、Key 来源、原生 TTS 冲突、平台、ffmpeg、缓存 |
| `/mimo帮助` | `mimo_help` | 使用帮助 |

---

## ⚠️ 常见问题排查（FAQ）

### Q1：WebUI 配置页「保存」按钮在手机上被挡住 / 点不到

这是 AstrBot WebUI 在移动端浏览器上的**布局遮挡**问题，不是本插件的 bug。插件配置项较多（33 项），页面较长，底部按钮可能被浏览器工具栏或浮动层遮住。

**解决办法（按推荐顺序）**：

1. **改用桌面浏览器**操作配置（最省事，手机端仅用于查看）；
2. 手机端**横屏**，或先向下滑动到底部再轻触按钮位置；
3. 把地址栏收起（部分浏览器上滑可隐藏工具栏）后再点；
4. 先随便改**任意一项**触发页面数据同步，再点保存，避免长表单未渲染完导致按钮坐标错位；
5. 实在点不到时，可直接修改配置文件（改完需重载插件）：
   ```
   /你的/AstrBot/data/config/astrbot_plugin_mimotts_config.json
   ```

> 修改配置后建议发送 `/mimo状态` 确认新配置已生效。

### Q2：QQ 有语音条，微信只收到一个 mp3 文件？

**这是预期行为，不是故障。**

- **QQ / NapCat / OneBot**：支持 `Record` 语音段，能真正发出「语音条」；
- **个人微信（`weixin_oc`）**：AstrBot 出站消息**只实现了 Image / Video / File**，没有语音段类型。插件会先尝试适配器私有通道构造语音（实验性），**失败即降级为 mp3 文件**，保证消息不丢。

如果只要语音条效果，请在 QQ 侧使用；微信侧收到可播放的音频文件已是当前最优解。

### Q3：长回复完全没有语音，日志也没报错？

大概率是被 `max_text_len` **静默拦截**了。

`max_text_len`（默认 `200`）的语义是「有效字数超过上限**直接跳过合成**，而不是截断朗读」。设计初衷是避免几万字长文被整段念出来。

**排查**：

1. 发送 `/mimo状态` 查看当前字数区间；
2. 若 LLM 回复普遍偏长，把 `max_text_len` 调到 `300 ~ 500`；
3. 设为 `0` 或负数表示**不限制**（慎用，长文本合成耗时长、费用高）；
4. 临时想看某条长回复的语音，用 `/mimo引用` 手动朗读。

> 同理，回复太短（如「嗯」「好的」）会被 `min_text_len`（默认 `2`）拦截。

### Q4：语音发了两次 / 被重复扣费？

**几乎必然是原生 TTS 与本插件同时生效**。请务必：

1. 按下方 [附录](#-附录关闭原生-tts避免双重扣费) 关闭 AstrBot 原生 TTS；
2. 保持 `auto_hook_skip_if_native_tts: true`（默认值）；
3. 若确实想同时保留，把 `send_mode` 设为 `voice_only`——原生 TTS 只处理 Plain 文本，本插件移除文本后它无从处理。

### Q5：报错「voiceclone 模型需要在插件配置中提供 voice_sample_path」

`model` 选了 `mimo-v2.5-tts-voiceclone`，但 `voice_sample_path` 为空或文件不存在。请：

1. 确认填写的是**绝对路径**；
2. 确认文件确实存在且有读权限；
3. 确认样本 base64 后 **< 10MB**；
4. 或先切回 `mimo-v2.5-tts` + `default_voice` 验证链路是否通畅。

### Q6：`/mimo状态` 显示 Key 未解析

按优先级逐个排查：

1. 原生 provider 是否真的配置了 MiMo TTS 且 `api_key` 非空；
2. 插件 `api_key` 是否填了（注意别带引号或空格）；
3. 环境变量 `MIMO_API_KEY` 是否在当前进程可见（**改完需重启 AstrBot**）；
4. `credentials_path` 指向的文件是否存在、格式是否为 `MIMO_API_KEY=sk-xxx` 或单行裸 Key。

### Q7：流式输出没有自动语音？

**预期行为**。流式模式下消息链尚未定稿，自动钩子会主动跳过，避免重复或错乱。需要使用流式时，可让模型调用 `mimo_speak` 工具，或关闭流式输出。

### Q8：语音延迟有 4~6 秒正常吗？

正常。MiMo 合成是同步非流式接口，延迟与文本长度正相关。如果觉得慢：

- 降低 `max_text_len`；
- 调低 `auto_speak_probability`（不是每条都合成）；
- 确保服务器到 MiMo 端点网络通畅。

---

## 📄 文件结构

```
astrbot_plugin_mimotts/
├── main.py             # 插件主逻辑（Star 子类 + 两个事件钩子）
├── metadata.yaml       # 插件元数据
├── _conf_schema.json   # 配置 Schema（33 项）
├── README.md           # 本文档
├── LICENSE             # MIT
└── tests/
    ├── test_units.py   # 28 项离线单测（纯函数 + 钩子行为，不联网）
    └── live_check.py   # 真实 API 联调自检（消耗额度，手动执行）
```

运行时数据目录：`data/plugin_data/astrbot_plugin_mimotts/`

- `cache/`：临时音频（TTL 清理）
- `session_state.json`：会话级音色 / 风格覆盖

---

## 🧪 自测

```bash
# 离线单测（28 项，无需网络，自带 runner，不依赖 pytest）
cd /你的/AstrBot && .venv/bin/python \
    data/plugins/astrbot_plugin_mimotts/tests/test_units.py

# 真实 API 联调（会消耗额度；验证合成与多端投递全链路）
cd /你的/AstrBot && .venv/bin/python \
    data/plugins/astrbot_plugin_mimotts/tests/live_check.py
```

单测覆盖：`<style>` 转换、Markdown 清理、有效字数、概率边界、提示词幂等注入、原生配置解析与 Key 优先级、端点归一化、自动钩子（追加/纯语音/概率/字数/去重/原生 TTS 冲突）、Schema 必填项校验、会话状态持久化、多端投递分支。

---

## 🔬 协议实现细节

```json
{
  "model": "mimo-v2.5-tts",
  "messages": [
    {"role": "user", "content": "自然亲切，语速中偏慢。"},
    {"role": "assistant", "content": "(温柔 撒娇)你好呀[轻笑]"}
  ],
  "audio": {"format": "wav", "voice": "冰糖"},
  "stream": false
}
```

- **Header**：`api-key: {key}`（官方 curl 写法）+ `Authorization: Bearer {key}`（兼容网关）
- **VoiceDesign**：`user.content` 即音色描述（必填），`audio = {format, optimize_text_preview}`，**不传 `voice`**
- **VoiceClone**：`audio.voice = "data:audio/mpeg;base64,..."`
- **响应**：`choices[0].message.audio.data`（base64 WAV，实测 24kHz / 单声道 / 16bit）
- **风格与导演指令**走 `role=user`（不发音），**朗读正文**走 `role=assistant`

---

## 📋 已知限制

- 流式输出（`STREAMING_*`）不触发自动发声，避免结果链未定稿时重复；
- 微信原生语音条依赖适配器私有方法（**实验性**），失败自动降级 mp3 文件；
- MiMo 单次合成建议 ≤ 300 字；自动钩子默认超过 200 字直接跳过；
- 非流式合成延迟约 4~6 秒（与文本长度相关）；
- 原生 TTS 与本插件功能重叠，**强烈建议只保留一个**（见附录）。

---

## 📚 附录：关闭原生 TTS（避免双重扣费）

### 为什么必须关

AstrBot 自带的 TTS 会在消息发送前再次遍历回复里的纯文本并合成语音。本插件同样会处理同一条回复，于是：

- **重合发声**：同一句话合成两遍，发两条语音；
- **双重扣费**：按字符计费时等于花两倍钱；
- **音色冲突**：原生 TTS 用自己的参数，覆盖本插件的 `(风格)` 与动作标签效果。

> 本插件对原生 TTS 配置是**只读复用**：只读取 `api_key` / `api_base`，**不会修改**你的 AstrBot 配置。所以关闭原生 TTS 不影响 Key 免配置能力。

### 方式一：管理面板（推荐）

1. 打开 AstrBot WebUI（默认 `http://<你的服务器IP>:6185`）；
2. 左侧进入 **「配置」→「服务提供商」**；
3. 切换到 **TTS（语音合成）** 分类：
   - 关闭 **「启用 TTS」** 总开关；或
   - 在 TTS 提供商列表中禁用 MiMo TTS provider（id 通常为 `mimo_tts`，类型 `mimo_tts_api`）；
4. 点击 **「保存」**；
5. 回到「插件管理」，重载本插件；
6. 发送 `/mimo状态` 确认：`原生 TTS：未启用（provider=-）`。

> 面板文案可能随版本略有差异，核心是找到 **TTS / 语音合成** 的启用开关与 MiMo TTS provider。

### 方式二：直接改配置文件

编辑 `data/cmd_config.json`（**建议先备份**）：

```jsonc
{
  "provider_tts_settings": {
    "enable": false,          // ← 关闭原生 TTS（关键）
    "provider_id": "mimo_tts",
    "dual_output": false,     // ← 关闭「文字+语音双输出」
    "use_file_service": false,
    "trigger_probability": 1.0
  }
}
```

修改后同样需要重载插件或重启 AstrBot。**本插件不会帮你改这个文件，请手动操作。**

### 暂时不想关原生 TTS？

| 做法 | 效果 |
|---|---|
| `send_mode = voice_only` | 原生 TTS 只处理 Plain 文本，本插件移除文本后它无从处理，**不会重合** |
| 保持 `auto_hook_skip_if_native_tts = true`（默认） | 「文字+语音」模式下本插件自动让位，只由原生 TTS 发声（但无本插件的标签/概率控制） |
| 关闭 `enable_auto_hook` | 只用 `/mimo` 与 `mimo_speak`，不参与自动发声 |

> ⚠️ 不要把 `auto_hook_skip_if_native_tts` 设为 `false` 且同时开启原生 TTS —— 那会双重扣费。

---

## 🗓️ 更新日志

### v1.1.0

- 新增 `on_decorating_result` 自动发声钩子（概率 / 字数 / Markdown 清理 / 文字+语音与纯语音两种模式）；
- 新增 `on_llm_request` 幂等导演提示词注入；
- 新增原生 TTS 凭据与端点自动复用（`cmd_config.json` 探测，带 60s 缓存）；
- 新增原生 TTS 冲突检测与自动让位，防双重扣费；
- 新增 `mimo_speak` 工具与自动钩子的三重去重；
- 配置项扩充至 33 项，新增 `default_style` / `min_text_len` / `max_text_len` 等；
- 单测扩充至 28 项。

### v1.0.0

- 首版：三模型合成、`<style>` 修复、QQ/微信双平台投递、指令体系、`mimo_speak` 工具。

---

## 📄 许可证与致谢

本项目采用 **MIT License** 开源，版权归 **Zyw052** 所有，详见 [LICENSE](LICENSE)。

### 致谢

- **[AstrBot](https://github.com/AstrBotDevs/AstrBot)** —— 优秀的开源聊天机器人框架，提供了稳定的插件体系与事件钩子；
- **小米 MiMo 团队** —— 提供高质量的中文语音合成能力与[开放平台](https://platform.xiaomimimo.com/)；
- **NapCat / OneBot 社区** —— 让语音条在 QQ 侧得以原生呈现；
- 以及所有提交 Issue、反馈问题的使用者。

### 免责声明

本项目为第三方非官方插件，与小米公司无隶属关系。语音合成服务由小米 MiMo 开放平台提供，其可用性、计费与政策以官方公告为准。请勿将音色克隆功能用于冒充他人、欺诈或任何违法用途。

### 贡献

欢迎 Issue 与 PR。提交前请先跑通 `tests/test_units.py`（28 项离线单测）。
