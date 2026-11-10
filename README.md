# astrbot_plugin_mimotts

小米 **MiMo-V2.5-TTS** 语音合成插件，支持 **微信（个人号）** 与 **NapCat QQ（OneBot v11）** 双平台直发原生语音条。

> 依据规范：`astrbot-plugin-dev` SKILL §2（目录与 metadata）、§3（main.py/Star）、§4（指令注册）、
> §5（消息发送）、§6（配置）、§9.2（数据目录）、§10.1（日志）、§10.2（aiohttp）、§10.3（ruff）。
> 协议依据：小米官方文档《Speech Synthesis (MiMo-V2.5-TTS Series)》。

---

## 1. 功能

- 调用 `mimo-v2.5-tts` / `mimo-v2.5-tts-voicedesign` / `mimo-v2.5-tts-voiceclone` 三种模型合成语音；
- 严格按官方协议组织 messages：
  - `role: user` → 风格 / 导演指令（**不发音**）；
  - `role: assistant` → `(风格)正文[动作标签]`，风格标签**必须在最开头**；
- **修复 Issue #7957**：输入中的 `<style>温柔 撒娇</style>你好呀` 会自动转换为官方语法
  `(温柔 撒娇)你好呀`，`<style>` 绝不会原样进入朗读正文；
- 动作标签自由插入：`[轻笑]`、`[叹气]`、`[撒娇]`、`[吸气]` 等；
- 多端语音直发：QQ 语音条 / 微信语音条（实验）/ 音频文件（自动降级）；
- 临时音频自动清理（TTL + 启动清扫 + 发送后延迟删除）；
- 提供 LLM 工具 `mimo_speak`，模型可主动发语音。

## 2. 文件结构

```
astrbot_plugin_mimotts/
├── main.py             # 插件主逻辑（Star 子类 MiMoTTSPlugin）
├── metadata.yaml       # 插件元数据（name/desc/version/author/repo）
├── _conf_schema.json   # 插件配置 schema（23 项）
├── README.md           # 本文档
└── tests/
    ├── test_units.py   # 纯函数 + 假事件投递单测（不依赖网络）
    └── live_check.py   # 真实 API 联调自检（消耗额度，手动执行）
```

运行时数据：`data/plugin_data/astrbot_plugin_mimotts/`
- `cache/`：临时音频（按 TTL 清理）
- `session_state.json`：会话级音色 / 风格覆盖

## 3. 配置项

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | true | 插件总开关 |
| `api_key` | string | 空 | MiMo Key；留空时回落环境变量 `MIMO_API_KEY` → `credentials_path` |
| `credentials_path` | string | `/data/apps/credentials/mimo.env` | 支持 `.env` 格式或单行裸 Key |
| `api_url` | string | `https://api.xiaomimimo.com/v1/chat/completions` | OpenAI 兼容端点 |
| `model` | string | `mimo-v2.5-tts` | 三种模型可选 |
| `default_voice` | string | `冰糖` | `mimo_default`/冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean |
| `voice_design_prompt` | text | 温婉女声… | VoiceDesign 音色描述（**必填**） |
| `voice_design_optimize_text` | bool | false | 对应官方 `optimize_text_preview` |
| `voice_sample_path` | string | 空 | VoiceClone 样本（mp3/wav，base64 < 10MB） |
| `voice_sample_format` | string | mp3 | 无扩展名时推断 MIME |
| `style_prefix` | string | 空 | 默认风格，如 `温柔 撒娇` |
| `style_instruction` | text | 自然亲切… | 放 `role=user` 的导演式指令 |
| `enable_audio_tags` | bool | true | 关闭则自动剥离 `[动作标签]` |
| `target_platforms` | list | aiocqhttp/weixin_oc/qq_official | 平台白名单，留空=全部 |
| `weixin_voice_mode` | string | auto | `auto`/`native_voice`/`file` |
| `weixin_voice_upload_type` | int | 3 | 微信 CDN 上传类型（实验通道） |
| `qq_record_format` | string | auto | `auto`/`silk`/`wav` |
| `send_text_caption` | bool | false | 先发文字再发语音 |
| `max_text_length` | int | 300 | 超长截断 |
| `request_timeout` | int | 90 | 请求超时（秒） |
| `enable_llm_tool` | bool | true | 注册 `mimo_speak` 工具 |
| `temp_ttl_seconds` | int | 900 | 临时音频保留时间 |
| `debug` | bool | false | 打印请求摘要 |

## 4. 指令

| 指令 | 别名 | 说明 |
|---|---|---|
| `/mimo <文本>` | `mimo语音`、`mimo说话` | 合成语音条 |
| `/mimo引用` | `mimo读引用`、`mimo_quote` | 朗读被引用的消息 |
| `/mimo音色 [名称]` | `mimo_voice` | 查看 / 切换本会话音色（`重置` 恢复） |
| `/mimo风格 [风格词]` | `mimo_style` | 查看 / 设置本会话默认风格（支持多词） |
| `/mimo状态` | `mimo_status` | 模型、Key、平台、ffmpeg、缓存状态 |
| `/mimo帮助` | `mimo_help` | 使用帮助 |

### 文本语法示例

```
/mimo <style>温柔 撒娇</style>主人今天辛苦了[轻笑]        → (温柔 撒娇)主人今天辛苦了[轻笑]
/mimo (慵懒)让我再睡五分钟嘛[叹气]                      → (慵懒)让我再睡五分钟嘛[叹气]
/mimo (唱歌)原谅我这一生不羁放纵爱自由                    → 唱歌模式
```

## 5. 协议实现细节

请求体：

```json
{
  "model": "mimo-v2.5-tts",
  "messages": [
    {"role": "user", "content": "自然亲切，语速中偏慢。", "NOTE": "不发音的导演指令"},
    {"role": "assistant", "content": "(温柔 撒娇)你好呀[轻笑]"}
  ],
  "audio": {"format": "wav", "voice": "冰糖"},
  "stream": false
}
```

- Header：`api-key: {key}`（官方 curl 写法）＋ `Authorization: Bearer {key}`（兼容网关）；
- VoiceDesign：`user.content` 即音色描述（必填），`audio` 为 `{format, optimize_text_preview}`，不传 `voice`；
- VoiceClone：`audio.voice = "data:audio/mpeg;base64,..."`；
- 响应：`choices[0].message.audio.data`（base64 WAV，实测 24kHz / 单声道 / 16bit）。

## 6. 多平台语音投递

| 平台 | 实现 | 结果 |
|---|---|---|
| `aiocqhttp`（NapCat / OneBot v11） | 发送 `Record` 段，框架转 base64 `record`；检测到 pysilk 时先编码 **Tencent SILK** | 原生语音条 |
| `weixin_oc`（个人微信） | AstrBot 出站仅实现 Image/Video/File，无 voice_item。插件先经适配器 `_prepare_media_item` 上传并重组 `voice_item` 投递（**实验性**），失败自动降级 | 语音条（若协议允许）/ mp3 文件 |
| `qq_official` 等 | 直接发 `Record`，框架转 tencent_silk | 原生语音条 |

> ⚠️ 微信说明：`weixin_oc` 适配器的 `send_by_session` 只处理 `Plain/ActionRow/Image/Video/File`，
> 直接发 `Record` 会被静默丢弃（框架 debug 日志 `unsupported outbound segment type Record`）。
> 因此本插件对微信采用「实验语音条 → 失败降级 mp3 文件」策略，绝不丢消息。
> `weixin_voice_mode=native_voice` 可强制只发语音条（失败会明确报错）。

音频处理链路：

```
MiMo WAV(24kHz/mono) → ffmpeg 统一 pcm_s16le/24k/mono → [pysilk] → .silk  (QQ)
                                                    └→ ffmpeg libmp3lame → .mp3 (微信降级)
```

ffmpeg 缺失时：直接使用 MiMo 原始 WAV（本身已是 24kHz/mono），并在 `/mimo状态` 中提示。

## 7. 安装与启用

1. 插件目录已就位：`data/plugins/astrbot_plugin_mimotts/`；
2. 依赖：`ffmpeg`（推荐）、`pysilk`（可选，QQ 兼容性更好）；`aiohttp` 由 AstrBot 自带；
3. 在 WebUI「插件管理」中确认插件已启用（本实例未开启 `ASTRBOT_RELOAD` 热重载，
   新增插件需重载插件列表或重启 AstrBot）；
4. 在插件配置中填写 `api_key`，或保留 `credentials_path` 指向 `mimo.env`；
5. 发送 `/mimo状态` 自检。

## 8. 自测

```bash
# 1) 离线单测（13 项，无需网络，自带 runner，不依赖 pytest）
cd /AstrBot && /AstrBot/.venv/bin/python \\
    data/plugins/astrbot_plugin_mimotts/tests/test_units.py

# 2) 真实 API 联调（会消耗额度；验证导入/实例化/合成/多端投递全链路）
cd /AstrBot && /AstrBot/.venv/bin/python \\
    data/plugins/astrbot_plugin_mimotts/tests/live_check.py
```

实测记录（本机）：
- 合成 `mimo-v2.5-tts` 4 秒语音 → 176KB WAV，耗时约 4~5s；
- 返回音频特性：`pcm_s16le / 24000Hz / 1ch / 16bit`；
- QQ 分支经 pysilk 编码 SILK（约 9.6KB），微信分支降级 mp3（约 20KB）。

## 9. 已知限制

- 微信原生语音条依赖适配器私有方法，属实验特性；微信协议侧若拒绝，会自动降级为 mp3 文件；
- MiMo 单次合成文本建议不超过 300 字，过长请分段；
- 流式（`pcm16`）未实现，插件使用非流式 `wav`，延迟 4~6 秒（4 秒语音实测）。
