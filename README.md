# astrbot_plugin_mimotts

小米 **MiMo-V2.5-TTS** 语音合成插件：**LLM 回复全自动发声** + **微信 / NapCat QQ 双平台原生语音条**。

> 版本：**v1.1.0**　｜　协议依据：小米官方文档《Speech Synthesis (MiMo-V2.5-TTS Series)》
> 规范依据：`astrbot-plugin-dev` SKILL（目录/metadata、Star、指令、消息发送、配置、数据目录、日志、aiohttp）

---

## 0. v1.1.0 亮点（全自动语音架构）

| 能力 | 说明 |
|---|---|
| 🔔 **全自动发声** | 通过 `@filter.on_decorating_result()` 拦截 LLM 最终回复，命中条件自动合成语音并拼接进消息链，**无需模型调用工具** |
| 🎲 **概率控制** | `auto_speak_probability`（0.0~1.0）随机决定是否发声 |
| 📏 **字数闸门** | `min_text_len` 过滤「嗯/好的」这类短回复；`max_text_len` 超过直接跳过，避免长时间播报 |
| 🧹 **Markdown 清理** | 自动剥离代码块、行内代码、链接/URL、加粗/斜体/删除线、标题、列表、引用、表格竖线，**不会把代码念出来** |
| 🎭 **隐式提示词注入** | `@filter.on_llm_request()` 幂等注入导演指导词，教 LLM 使用 `(风格)` 与 `[轻笑]` 等标签，不破坏原人设 |
| 🔑 **Key 免二次配置** | 自动读取 AstrBot `cmd_config.json` 中已配置的 MiMo TTS provider 的 `api_key` / `api_base` |
| 🛡️ **防双重扣费** | 检测到原生 TTS 仍启用时，「文字+语音」模式自动让位并告警，避免同一句合成两次 |
| 🚫 **三重去重** | 流式输出跳过、结果链已有语音段跳过、本轮 `mimo_speak` 已发声跳过 |

---

## 1. 功能总览

- 三种模型：`mimo-v2.5-tts`（内置音色）/ `...-voicedesign`（文字描述生成音色）/ `...-voiceclone`（音频样本复刻）；
- 严格按官方协议组织 messages：
  - `role: user` → 风格 / 导演指令（**不发音**）；
  - `role: assistant` → `(风格)正文[动作标签]`，风格标签**必须在最开头**；
- **修复 Issue #7957**：`<style>温柔 撒娇</style>你好呀` 自动转换为 `(温柔 撒娇)你好呀`，`<style>` 绝不进入朗读正文；
- 动作标签自由插入：`[轻笑]`、`[叹气]`、`[撒娇]`、`[吸气]` 等；
- 多端语音直发：QQ 原生语音条 / 微信语音条（实验）/ 音频文件（自动降级）；
- 临时音频自动清理（TTL + 启动清扫 + 发送后延迟删除）；
- 保留 LLM 工具 `mimo_speak`（模型可主动发语音，与自动钩子自动去重）。

---

## 2. 自动发声工作原理

```
用户消息
   │
   ├─[on_llm_request]──► 幂等注入导演提示词（教 LLM 用 (风格) 与 [轻笑]）
   │
LLM 生成回复
   │
   └─[on_decorating_result]──► 自动发声闸门
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
         合成 WAV → 构造语音组件
              ↓
     send_mode = voice_with_text → 文字 + 语音条
     send_mode = voice_only      → 仅语音条（保留图片/引用等非文本组件）
```

> 任何一步不通过都**保持原有纯文本回复**，钩子异常会被捕获并记录，绝不影响正常聊天。

### 2.1 有效字数怎么算

- 只统计「会发音」的字符：忽略空白与 `[动作标签]`；
- 例：`[轻笑]好的` → 有效 2 字；`嗯` → 1 字（默认 `min_text_len=2` 会跳过）。

### 2.2 为什么不会重复发声

| 场景 | 处理 |
|---|---|
| 模型调用 `mimo_speak` 后再输出总结 | 工具成功即打事件标记，钩子跳过 |
| AstrBot 原生 TTS / 其它插件已加语音段 | 结果链检测到 `Record` 即跳过 |
| 流式输出 | 主动跳过（流式下结果链尚未定稿） |
| `/mimo` 等指令输出 | 非 LLM 结果，钩子不处理 |

---

## 3. ⚠️ 如何关闭 AstrBot 原生 TTS（避免双重扣费与重合发声）

### 3.1 为什么必须关

AstrBot 自带的 TTS 功能会在消息发送前再次遍历回复里的**纯文本**并合成语音。本插件同样会处理同一条回复，于是：

- **重合发声**：同一句话被合成两遍、发两条语音；
- **双重扣费**：MiMo 按字符计费，等于花两倍钱；
- **音色冲突**：原生 TTS 用自己的音色/风格参数，覆盖本插件的 `(风格)` 与动作标签效果。

> 本插件对原生 TTS 配置是**只读复用**：只读取其中的 `api_key` / `api_base`，
> **不会修改**你的 AstrBot 配置。所以「关闭原生 TTS」不影响 Key 免配置能力。

### 3.2 方式一：管理面板操作（推荐）

1. 浏览器打开 AstrBot 管理面板（默认 `http://<你的服务器IP>:6185`）；
2. 左侧进入 **「配置」→「服务提供商」**（部分版本叫「服务提供商 / Provider」）；
3. 切换到 **TTS（语音合成）** 分类：
   - 把 **「启用 TTS」** 总开关关闭；或
   - 在 TTS 提供商列表里，找到 MiMo TTS（provider id 通常为 `mimo_tts`，类型 `mimo_tts_api`），
     将其 **禁用（enable=false）** 或直接删除；
4. 点击 **「保存」**；
5. 回到「插件管理」，对本插件执行 **重载**（或重载全部插件），使自动发声配置生效；
6. 发送 `/mimo状态` 检查：
   ```
   · 原生 TTS：未启用（provider=-）
   · 自动发声：开启（概率 0.60，模式 voice_with_text，字数 2~200）
   ```

> 面板文案可能随版本略有差异，核心是找到 **TTS / 语音合成** 的启用开关与 MiMo TTS provider。

### 3.3 方式二：直接改配置文件

编辑 AstrBot 数据目录下的 `data/cmd_config.json`（建议先备份）：

```jsonc
{
  "provider_tts_settings": {
    "enable": false,          // ← 关闭原生 TTS（关键）
    "provider_id": "mimo_tts",
    "dual_output": false,     // ← 关闭「文字+语音双输出」，彻底避免重复
    "use_file_service": false,
    "trigger_probability": 1.0
  }
}
```

若希望彻底移除原生 MiMo TTS，可在 `provider` 数组中删除
`provider_type == "text_to_speech"` 且 id/type 含 `mimo` 的那一项（**保留 `provider_sources` 中的记录不影响本插件探测**）。

修改后同样需要重载插件或重启 AstrBot。**本插件不会帮你改这个文件，请手动操作。**

### 3.4 如果暂时不想关原生 TTS

| 做法 | 效果 |
|---|---|
| 保持 `send_mode = voice_only` | 原生 TTS 只处理 Plain 文本，本插件移除文本后它无从处理，**不会重合** |
| 保持 `auto_hook_skip_if_native_tts = true`（默认） | 「文字+语音」模式下本插件自动让位，只由原生 TTS 发声（无本插件的标签/概率控制） |
| 关闭本插件 `enable_auto_hook` | 只用 `/mimo` 指令与 `mimo_speak` 工具，不参与自动发声 |

> 不推荐把 `auto_hook_skip_if_native_tts` 设为 `false` 且同时开启原生 TTS —— 那会双重扣费。

---

## 4. 配置项详解

### 4.1 自动语音

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `true` | 插件总开关（指令 + 工具 + 钩子） |
| `enable_auto_hook` | bool | `true` | 全局自动发声开关 |
| `auto_speak_probability` | float | `0.6` | 触发概率，0.0~1.0；1.0=每条都发，0.0=从不 |
| `send_mode` | string | `voice_with_text` | `voice_with_text`=文字+语音；`voice_only`=纯语音 |
| `default_style` | string | `温柔 撒娇` | 兜底风格；会话风格与文本内风格优先 |
| `enable_prompt_injection` | bool | `true` | 是否注入导演提示词 |
| `prompt_injection_text` | text | 空 | 留空用内置指导词 |
| `min_text_len` | int | `2` | 自动发声最小有效字数，小于则跳过 |
| `max_text_len` | int | `200` | 自动发声最大有效字数，大于则**跳过**（不截断） |
| `auto_hook_skip_if_native_tts` | bool | `true` | 原生 TTS 启用时让位，防双重扣费 |
| `native_config_path` | string | 空 | 手动指定 `cmd_config.json`；留空自动探测 |

### 4.2 凭据与端点

**凭据优先级（v1.1.0）**：

1. AstrBot 全局配置 `cmd_config.json` 中原生 MiMo TTS provider 的 `api_key` ✅ 推荐，免二次配置
2. 插件配置 `api_key`
3. 环境变量 `MIMO_API_KEY`
4. 凭证文件 `credentials_path`（默认 `/data/apps/credentials/mimo.env`，支持 `.env` 或单行裸 Key）

**端点优先级**：插件显式 `api_url`（非默认值）→ 原生 provider 的 `api_base`（自动补 `/chat/completions`）→ 内置默认端点。

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `api_key` | string | 空 | 留空即自动复用原生 Key |
| `credentials_path` | string | `/data/apps/credentials/mimo.env` | 兜底凭证文件 |
| `api_url` | string | `https://api.xiaomimimo.com/v1/chat/completions` | 保持默认即自动复用原生 `api_base` |
| `model` | string | `mimo-v2.5-tts` | 三种模型可选 |
| `request_timeout` | int | `90` | 请求超时（秒） |

### 4.3 音色与风格

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `default_voice` | string | `冰糖` | 下拉：冰糖 / 茉莉 / 苏打 / 白桦 / Mia / Chloe / Milo / Dean（另含别名 `mimo_default`） |
| `voice_design_prompt` | text | 温婉女声… | VoiceDesign 必填音色描述 |
| `voice_design_optimize_text` | bool | `false` | 官方 `optimize_text_preview` |
| `voice_sample_path` | string | 空 | VoiceClone 样本（mp3/wav，base64 < 10MB） |
| `voice_sample_format` | string | `mp3` | 无扩展名时推断 MIME |
| `style_prefix` | string | 空 | 旧版默认风格，优先级高于 `default_style` |
| `style_instruction` | text | 自然亲切… | 放 `role=user` 的导演式指令 |
| `enable_audio_tags` | bool | `true` | 关闭则自动剥离 `[动作标签]` |

### 4.4 平台与发送

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `target_platforms` | list | `aiocqhttp`/`weixin_oc`/`qq_official` | 平台白名单，留空=全部 |
| `weixin_voice_mode` | string | `auto` | `auto`/`native_voice`/`file`（自动钩子统一按 auto 语义降级为文件） |
| `weixin_voice_upload_type` | int | `3` | 微信 CDN 上传类型（实验通道） |
| `qq_record_format` | string | `auto` | `auto`/`silk`/`wav` |
| `send_text_caption` | bool | `false` | 仅对 `/mimo` 与 `mimo_speak` 有效 |
| `max_text_length` | int | `300` | 仅对 `/mimo` 与 `mimo_speak` 有效（超长截断） |
| `enable_llm_tool` | bool | `true` | 注册 `mimo_speak` 工具 |
| `temp_ttl_seconds` | int | `900` | 临时音频保留时间 |
| `debug` | bool | `false` | 打印请求摘要与闸门判定日志 |

---

## 5. 导演提示词与标签语法

### 5.1 注入内容（可通过 `prompt_injection_text` 覆盖）

> 你可以在回复开头附带 (风格) 前缀（如 开心/温柔/撒娇/低语/严肃），并在语句停顿处适度插入
> `[轻笑]`、`[叹气]`、`[撒娇]`、`[吸气]` 等动作标签，使语气更加生动逼真。
> 注意：正文不要包含 Markdown 代码块或复杂排版。

注入特性：

- 幂等：`system_prompt` 中已含 `[MiMo-TTS]` 标记则不再追加；
- 保留原人设：只**追加**到末尾，不替换任何已有内容；
- 仅在「插件启用 + 自动发声开启 + 注入开关开启 + 平台命中」时生效。

### 5.2 风格标签

| 写法 | 说明 |
|---|---|
| `(温柔 撒娇)你好呀` | 官方语法，必须位于正文**最开头** |
| `（慵懒）让我再睡五分钟` | 全角括号同样支持 |
| `<style>清冷</style>别说话` | 自动转换为 `(清冷)别说话` |
| `(唱歌)原谅我这一生不羁放纵爱自由` | 唱歌风格 |

### 5.3 动作标签清单

标签插在任意停顿处，由 MiMo 模型渲染为气声、笑声、叹气等细节：

| 类别 | 常用标签 |
|---|---|
| 笑 | `[轻笑]` `[大笑]` `[笑]` |
| 呼吸 | `[叹气]` `[长叹]` `[吸气]` `[深呼吸]` `[打哈欠]` |
| 情绪 | `[撒娇]` `[娇嗔]` `[哽咽]` `[抽泣]` `[哭]` `[颤抖]` |
| 音量/语气 | `[小声]` `[喃喃]` `[低语]` `[耳语]` `[停顿]` |
| 其它 | `[咳嗽]` |
| 英文兼容 | `[whisper]` `[laugh]` `[sigh]` |

> `enable_audio_tags=false` 时，这些标签会在合成前被剥离（正文照常朗读）。

---

## 6. 双平台表现

| 平台 | 自动钩子行为 | 结果 |
|---|---|---|
| `aiocqhttp`（NapCat / OneBot v11） | 追加 `Record` 段，框架转 base64 `record`；检测到 pysilk 时先编码 **Tencent SILK** | 原生语音条（文字+语音 或 纯语音） |
| `weixin_oc`（个人微信） | 出站不支持 `Record` → 自动转 **mp3 文件**（`File` 段） | 可播放的音频文件，不丢消息 |
| `qq_official` 等 | 追加 `Record`，由框架转 tencent_silk | 原生语音条 |

音频处理链路：

```
MiMo WAV(24kHz/mono) → ffmpeg 统一 pcm_s16le/24k/mono ─┬→ [pysilk] → .silk  (QQ)
                                                       └→ libmp3lame → .mp3  (微信降级)
```

ffmpeg 缺失时直接使用 MiMo 原始 WAV（本身已是 24kHz/mono），`/mimo状态` 会提示。

---

## 7. 指令

| 指令 | 别名 | 说明 |
|---|---|---|
| `/mimo <文本>` | `mimo语音`、`mimo说话` | 合成语音条 |
| `/mimo引用` | `mimo读引用`、`mimo_quote` | 朗读被引用的消息 |
| `/mimo音色 [名称]` | `mimo_voice` | 查看 / 切换本会话音色（`重置` 恢复） |
| `/mimo风格 [风格词]` | `mimo_style` | 查看 / 设置本会话默认风格（支持多词） |
| `/mimo状态` | `mimo_status` | 自动发声、Key 来源、原生 TTS 冲突、平台、ffmpeg、缓存 |
| `/mimo帮助` | `mimo_help` | 使用帮助 |

示例：

```
/mimo <style>温柔 撒娇</style>主人今天辛苦了[轻笑]
/mimo (慵懒)让我再睡五分钟嘛[叹气]
/mimo (唱歌)原谅我这一生不羁放纵爱自由
```

---

## 8. 协议实现细节

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

- Header：`api-key: {key}`（官方 curl 写法）＋ `Authorization: Bearer {key}`（兼容网关）；
- VoiceDesign：`user.content` 即音色描述（必填），`audio = {format, optimize_text_preview}`，不传 `voice`；
- VoiceClone：`audio.voice = "data:audio/mpeg;base64,..."`；
- 响应：`choices[0].message.audio.data`（base64 WAV，实测 24kHz / 单声道 / 16bit）。

---

## 9. 文件结构

```
astrbot_plugin_mimotts/
├── main.py             # 插件主逻辑（Star 子类 + 两个事件钩子）
├── metadata.yaml       # 插件元数据（v1.1.0）
├── _conf_schema.json   # 配置 schema（33 项）
├── README.md           # 本文档
└── tests/
    ├── test_units.py   # 28 项离线单测（纯函数 + 钩子行为，不联网）
    └── live_check.py   # 真实 API 联调自检（消耗额度，手动执行）
```

运行时数据：`data/plugin_data/astrbot_plugin_mimotts/`
- `cache/`：临时音频（TTL 清理）
- `session_state.json`：会话级音色 / 风格覆盖

---

## 10. 安装与启用

1. 插件目录就位：`data/plugins/astrbot_plugin_mimotts/`；
2. 依赖：`ffmpeg`（推荐）、`pysilk`（可选）；`aiohttp` 由 AstrBot 自带；
3. 在 AstrBot 面板启用插件，并**按第 3 章关闭原生 TTS**；
4. 凭据：若面板里已配置过 MiMo TTS provider，则无需再填 `api_key`；否则填插件 `api_key` 或写 `mimo.env`；
5. 发送 `/mimo状态` 自检；
6. 配置 `auto_speak_probability`、`send_mode`、字数区间到满意值。

---

## 11. 自测

```bash
# 离线单测（28 项，无需网络，自带 runner，不依赖 pytest）
cd /AstrBot && /AstrBot/.venv/bin/python \
    data/plugins/astrbot_plugin_mimotts/tests/test_units.py

# 真实 API 联调（会消耗额度；验证合成与多端投递全链路）
cd /AstrBot && /AstrBot/.venv/bin/python \
    data/plugins/astrbot_plugin_mimotts/tests/live_check.py
```

单测覆盖：`<style>` 转换、Markdown 清理、有效字数、概率边界、提示词幂等注入、
原生配置解析与 Key 优先级、端点归一化、自动钩子（追加/纯语音/概率/字数/去重/原生 TTS 冲突）、
Schema 必填项校验、会话状态持久化、多端投递分支。

---

## 12. 已知限制

- 流式输出（`STREAMING_*`）不触发自动发声，避免结果链未定稿时重复；
- 微信原生语音条依赖适配器私有方法（实验性），失败自动降级 mp3 文件；
- MiMo 单次合成建议 ≤ 300 字；自动钩子默认超过 200 字直接跳过；
- 非流式合成延迟约 4~6 秒（与文本长度相关）；
- 原生 TTS 与本插件功能重叠，**强烈建议只保留一个**（见第 3 章）。

---

## 13. 更新日志

### v1.1.0

- 新增 `on_decorating_result` 自动发声钩子（概率 / 字数 / Markdown 清理 / 文字+语音与纯语音两种模式）；
- 新增 `on_llm_request` 幂等导演提示词注入；
- 新增原生 TTS 凭据与端点自动复用（`cmd_config.json` 探测，带 60s 缓存）；
- 新增原生 TTS 冲突检测与自动让位，防双重扣费；
- 新增 `mimo_speak` 工具与自动钩子的三重去重；
- 配置项扩充至 33 项，`default_style` / `min_text_len` / `max_text_len` 等；
- 单测扩充至 28 项。

### v1.0.0

- 首版：三模型合成、`<style>` 修复、QQ/微信双平台投递、指令体系、`mimo_speak` 工具。
