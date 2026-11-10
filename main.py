"""AstrBot 小米 MiMo TTS 插件（微信 + NapCat QQ 双平台原生语音条）

功能：
1. 调用小米 MiMo-V2.5-TTS 系列模型合成语音（内置音色 / VoiceDesign / VoiceClone）；
2. 严格按官方协议组织 messages：
   - role=user 放「风格 / 导演指令」自然语言（不发音）；
   - role=assistant 放 `(风格)正文[动作标签]`，风格标签一律置于最开头；
   - 文本里的 `<style>...</style>` 会被转换成官方 `(风格)` 语法（修复 Issue #7957）；
3. 多端语音直发：
   - NapCat / OneBot v11（aiocqhttp）：发送 `Record` 语音段，框架转 base64 交给 NapCat 转 silk，
     QQ 端呈现为语音条；若环境具备 pysilk 也可自行编码 silk；
   - 个人微信（weixin_oc）：框架出站原生不支持 voice 段，插件会先尝试用适配器私有通道
     构造 voice_item（实验性），失败自动降级为音频文件（File），保证消息不丢；
   - 其它平台（qq_official 等）：直接发送 `Record`，由框架自行转换；
4. 临时音频文件统一落在 `data/plugin_data/astrbot_plugin_mimotts/cache/`，
   按 TTL 延迟清理，并在插件启动时清扫过期文件。

v1.1.0 新增（全自动语音架构）：
5. 全局自动发声钩子：`@filter.on_decorating_result()` 拦截 LLM 最终回复文本，
   按「概率 / 字数区间 / 去 Markdown」三重闸门判断是否合成语音，并把语音段
   拼接进消息链（文字+语音并存，或纯语音），无需模型主动调用工具；
6. 隐式导演提示词注入：`@filter.on_llm_request()` 幂等追加轻量指导词，
   教 LLM 使用 `(风格)` 前缀与 `[轻笑]` 等动作标签，不破坏原有人设；
7. 原生 TTS 凭据复用：优先解析 AstrBot `cmd_config.json` 中已配置的 MiMo TTS
   provider（api_key / api_base），实现免二次配 Key 的开箱即用；
8. 防双重发声：检测到原生 TTS 仍启用时，在「文字+语音」模式下自动让位并告警，
   避免同一句话被合成两次（双重扣费）。

依据规范：
- 插件结构 / metadata / 配置：astrbot-plugin-dev SKILL §2 / §3 / §6；
- 指令注册 `@filter.command`：§4；
- 消息发送（MessageChain / Record / event.send）：§5；
- 日志 `from astrbot.api import logger`：§10.1；异步网络 aiohttp：§10.2；
- 持久化目录 `StarTools.get_data_dir()`：§9.2。

MiMo 协议依据：官方文档《Speech Synthesis (MiMo-V2.5-TTS Series)》
（messages 结构、`(风格)` 标签、`[动作标签]`、`audio.format/voice`、返回 base64 audio）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.star.filter.command import GreedyStr

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

PLUGIN_NAME = "astrbot_plugin_mimotts"

DEFAULT_API_URL = "https://api.xiaomimimo.com/v1/chat/completions"
DEFAULT_CREDENTIALS_PATH = "/data/apps/credentials/mimo.env"

# ---- 自动发声 / 提示词注入 ----

#: 注入到 system_prompt 的幂等标记：已存在则不再重复注入
PROMPT_INJECTION_MARKER = "[MiMo-TTS]"

#: 默认导演提示词（教 LLM 使用 MiMo 官方风格/动作标签语法）
DEFAULT_PROMPT_INJECTION = (
    "你可以在回复开头附带 (风格) 前缀（如 开心/温柔/撒娇/低语/严肃），"
    "并在语句停顿处适度插入 [轻笑]、[叹气]、[撒娇]、[吸气] 等动作标签，"
    "使语气更加生动逼真。注意：正文不要包含 Markdown 代码块或复杂排版。"
)

#: 事件级标记：本轮已由 mimo_speak 工具发过语音，自动钩子不再重复发声
HOOK_SPOKEN_EXTRA_KEY = "mimotts_auto_hook_spoken"

#: 发送模式
SEND_MODE_WITH_TEXT = "voice_with_text"
SEND_MODE_VOICE_ONLY = "voice_only"
SEND_MODES = (SEND_MODE_WITH_TEXT, SEND_MODE_VOICE_ONLY)

#: 自动钩子监听的原生 TTS 冲突告警节流（秒）
NATIVE_TTS_WARN_INTERVAL = 300.0

MODEL_TTS = "mimo-v2.5-tts"
MODEL_VOICEDESIGN = "mimo-v2.5-tts-voicedesign"
MODEL_VOICECLONE = "mimo-v2.5-tts-voiceclone"
MODEL_CHOICES = (MODEL_TTS, MODEL_VOICEDESIGN, MODEL_VOICECLONE)

# 官方内置音色（中文 4 + 英文 4 + 默认别名）
VOICE_CHOICES = (
    "mimo_default",
    "冰糖",
    "茉莉",
    "苏打",
    "白桦",
    "Mia",
    "Chloe",
    "Milo",
    "Dean",
)

# 常见动作标签（用于 enable_audio_tags=False 时剥离；同时用于文档提示）
COMMON_AUDIO_TAGS = (
    "轻笑",
    "大笑",
    "笑",
    "叹气",
    "长叹",
    "吸气",
    "深呼吸",
    "撒娇",
    "娇嗔",
    "哽咽",
    "抽泣",
    "哭",
    "小声",
    "喃喃",
    "停顿",
    "咳嗽",
    "打哈欠",
    "颤抖",
    "低语",
    "耳语",
    "whisper",
    "laugh",
    "sigh",
)

# `<style>...</style>` / `<风格>...</风格>`（Issue #7957 兼容写法）
_STYLE_TAG_PATTERNS = (
    re.compile(r"<\s*style\s*>(.*?)<\s*/\s*style\s*>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<\s*风格\s*>(.*?)<\s*/\s*风格\s*>", re.DOTALL),
    re.compile(r"<\s*style\s*>(.*)$", re.DOTALL | re.IGNORECASE),  # 只有开标签的容错
)

# 正文开头的 `(风格)` / `（风格）`（不含 `[..]`：方括号优先解释为动作标签）
_LEADING_STYLE_PATTERN = re.compile(r"^\s*[\(（]([^()（）]{1,40})[\)）]\s*")

# 任意位置的 [动作标签]（半角/全角方括号）
_AUDIO_TAG_PATTERN = re.compile(r"[\[［]([^\[\]［］\s]{1,12})[\]］]")

# 需要从朗读文本中剔除的内容
_CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"`([^`]*)`")
_URL_PATTERN = re.compile(r"https?://\S+")
_MD_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_PATTERN = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HTML_TAG_PATTERN = re.compile(r"</?[A-Za-z][^>]{0,60}>")

# 空白字符用显式码点构造，避免源码中直接出现控制字符转义
_TAB = chr(9)
_IDEOGRAPHIC_SPACE = chr(0x3000)
_CR = chr(13)
_LF = chr(10)
# 引号集合（单引号 + 双引号），用于 strip 首尾引号
_QUOTES = chr(39) + chr(34)

_WS_PATTERN = re.compile(f"[ {_TAB}{_IDEOGRAPHIC_SPACE}]+")
_MULTI_NL_PATTERN = re.compile(_LF + "{2,}")


# --------------------------------------------------------------------------- #
# 纯函数工具（便于单测）
# --------------------------------------------------------------------------- #


def clean_text(raw: str) -> str:
    """清理不适合朗读的标记（代码块 / 链接 / 图片 / HTML / 多余空白）。

    Args:
        raw: 原始文本。

    Returns:
        清理后的纯文本（不含首尾空白）。
    """
    if not raw:
        return ""
    text = str(raw)
    text = _CODE_FENCE_PATTERN.sub(" ", text)
    text = _INLINE_CODE_PATTERN.sub(r"\1", text)
    text = _MD_IMAGE_PATTERN.sub(" ", text)
    text = _MD_LINK_PATTERN.sub(r"\1", text)
    text = _URL_PATTERN.sub(" ", text)
    text = _HTML_TAG_PATTERN.sub(" ", text)
    text = text.replace(_CR + _LF, _LF).replace(_CR, _LF)
    text = _WS_PATTERN.sub(" ", text)
    text = _MULTI_NL_PATTERN.sub("\n", text)
    return text.strip()


def extract_style_and_body(raw: str) -> tuple[str, str]:
    """从原始文本中提取风格与正文（修复 Issue #7957）。

    将 `<style>温柔 撒娇</style>你好呀` 这类写法转成官方要求的
    `(温柔 撒娇)你好呀` 语义：风格单独返回，正文不含标签。

    Args:
        raw: 原始文本，可能含有 `<style>...</style>`。

    Returns:
        (style, body)：风格字符串（可能为空）与清理后的正文。
    """
    if not raw:
        return "", ""
    text = str(raw)
    styles: list[str] = []

    for pattern in _STYLE_TAG_PATTERNS:

        def _collect(match: re.Match[str]) -> str:
            part = clean_text(match.group(1) or "")
            if part:
                styles.append(part)
            return " "

        text = pattern.sub(_collect, text)

    # 正文自带的开头 `(风格)` 同样视为风格，避免重复拼接
    body = clean_text(text)
    leading = _LEADING_STYLE_PATTERN.match(body)
    if leading:
        part = clean_text(leading.group(1))
        if part:
            styles.append(part)
            body = body[leading.end() :].lstrip()

    # 去重并保持顺序
    seen: set[str] = set()
    ordered: list[str] = []
    for item in styles:
        key = item.replace(" ", "")
        if key and key not in seen:
            seen.add(key)
            ordered.append(item)

    return " ".join(ordered), body.strip()


def strip_audio_tags(text: str) -> str:
    """移除全部 `[动作标签]`（enable_audio_tags=False 时使用）。

    Args:
        text: 待处理文本。

    Returns:
        不含动作标签的文本。
    """
    if not text:
        return ""
    return _AUDIO_TAG_PATTERN.sub("", text).strip()


def compose_assistant_content(
    body: str,
    style: str = "",
    *,
    enable_audio_tags: bool = True,
) -> str:
    """拼装 `role=assistant` 的朗读文本：`(风格)正文[动作标签]`。

    官方要求风格标签必须位于目标文本的最开头，因此这里统一做前缀处理：
    若 style 非空，则生成为 `(style)body`；若 body 开头已带 `(x)` 则不再重复添加。

    Args:
        body: 正文。
        style: 风格字符串，多个风格用空格分隔。
        enable_audio_tags: 是否保留 `[动作标签]`。

    Returns:
        最终朗读文本。
    """
    text = body or ""
    if not enable_audio_tags:
        text = strip_audio_tags(text)

    style_text = (style or "").strip()
    if not style_text:
        return text

    # 正文本身已带开头风格标签则不重复
    if _LEADING_STYLE_PATTERN.match(text):
        existing = _LEADING_STYLE_PATTERN.match(text)
        if existing and clean_text(existing.group(1)) == clean_text(style_text):
            return text

    return f"({style_text}){text}"


def clamp_text(text: str, max_length: int) -> tuple[str, bool]:
    """按最大长度截断文本。

    Args:
        text: 原始文本。
        max_length: 最大字符数（<=0 表示不限制）。

    Returns:
        (截断后的文本, 是否发生截断)
    """
    if max_length and max_length > 0 and len(text) > max_length:
        return text[:max_length], True
    return text, False


def parse_env_file(
    path: str | Path, keys: tuple[str, ...] = ("MIMO_API_KEY", "API_KEY")
) -> str:
    """从 .env 风格文件解析 API Key。

    Args:
        path: 文件路径。
        keys: 候选键名，按顺序匹配。

    Returns:
        找到的 Key；未找到返回空字符串。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:  # 权限 / IO 异常不致命
        logger.debug(f"[mimotts] 读取凭证文件失败 {file_path}: {exc}")
        return ""

    values: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip().strip(_QUOTES)

    for key in keys:
        value = values.get(key.upper(), "")
        if value:
            return value
    # 退化：整个文件就是一个裸 Key
    return (
        content.strip().strip(_QUOTES) if len(content.strip().splitlines()) == 1 else ""
    )


# --------------------------------------------------------------------------- #
# 自动发声 / 提示词注入 / 原生配置复用（纯函数，便于离线单测）
# --------------------------------------------------------------------------- #

# Markdown 结构标记（用于朗读前的清理，避免把代码/链接/排版读出来）
_SPEECH_UNCLOSED_FENCE = re.compile(r"```.*$", re.DOTALL)
_SPEECH_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_SPEECH_BOLD = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_SPEECH_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\*)([^*\n]+)\*(?!\*)")
_SPEECH_ITALIC_UNDER = re.compile(r"(?<!_)_(?!_)([^_\n]+)_(?!_)")
_SPEECH_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_SPEECH_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_SPEECH_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_SPEECH_LIST = re.compile(r"^\s{0,3}(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_SPEECH_HR = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", re.MULTILINE)
_SPEECH_TABLE_SEP = re.compile(
    r"^\s*\|?(?:\s*:?-{2,}:?\s*\|)+\s*$", re.MULTILINE
)
_SPEECH_TABLE_PIPE = re.compile(r"\s*\|\s*")
_SPEECH_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
_SPEECH_WS = re.compile(r"[ \t\u3000]+")
_SPEECH_NEWLINE_WRAP = re.compile(r"[ \t\u3000]*\n[ \t\u3000]*")


def strip_markdown_for_speech(raw: str) -> str:
    """把 LLM 回复清洗成适合朗读的纯文本。

    处理范围：
    - 代码块 / 行内代码（保留行内代码文字，丢弃围栏）；
    - 图片、链接（保留链接文字，丢弃 URL）；
    - 加粗 `**x**` / `__x__`、斜体 `*x*` / `_x_`、删除线 `~~x~~`；
    - 标题 `#`、引用 `>`、列表 `- 1.`、分隔线、表格竖线；
    - HTML 注释与标签、Markdown 转义反斜杠；
    - 连续空白压缩。
    注意：`[轻笑]` 这类动作标签会**原样保留**，交给 MiMo 模型处理。

    Args:
        raw: LLM 原始回复文本。

    Returns:
        适合朗读的纯文本（可能为空）。
    """
    if not raw:
        return ""
    text = str(raw)
    text = _SPEECH_HTML_COMMENT.sub(" ", text)
    text = _CODE_FENCE_PATTERN.sub(" ", text)
    text = _SPEECH_UNCLOSED_FENCE.sub(" ", text)
    text = _INLINE_CODE_PATTERN.sub(r"\1", text)
    text = _MD_IMAGE_PATTERN.sub(" ", text)
    text = _MD_LINK_PATTERN.sub(r"\1", text)
    text = _SPEECH_BOLD.sub(r"\2", text)
    text = _SPEECH_STRIKE.sub(r"\1", text)
    text = _SPEECH_ITALIC_STAR.sub(r"\1", text)
    text = _SPEECH_ITALIC_UNDER.sub(r"\1", text)
    text = _SPEECH_HEADING.sub("", text)
    text = _SPEECH_QUOTE.sub("", text)
    text = _SPEECH_LIST.sub("", text)
    text = _SPEECH_HR.sub(" ", text)
    text = _SPEECH_TABLE_SEP.sub(" ", text)
    text = _SPEECH_TABLE_PIPE.sub(" ", text)
    text = _SPEECH_ESCAPE.sub(r"\1", text)
    text = _URL_PATTERN.sub(" ", text)
    text = _HTML_TAG_PATTERN.sub(" ", text)
    text = text.replace(_CR + _LF, _LF).replace(_CR, _LF)
    text = _SPEECH_WS.sub(" ", text)
    text = _SPEECH_NEWLINE_WRAP.sub("\n", text)
    text = _MULTI_NL_PATTERN.sub("\n", text)
    return text.strip()


def speech_length(text: str) -> int:
    """计算「有效发音长度」：忽略空白与动作标签。

    动作标签（如 `[轻笑]`）本身不计入字数，但仍会传给模型发声，
    因此 `[轻笑]嗯` 的有效长度是 1，`好的` 是 2。

    Args:
        text: 已清理的朗读文本。

    Returns:
        有效字符数。
    """
    if not text:
        return 0
    plain = strip_audio_tags(str(text))
    return len("".join(plain.split()))


def prepare_speech_candidate(
    raw: str,
    *,
    min_len: int = 2,
    max_len: int = 200,
) -> tuple[str, str]:
    """把 LLM 文本整理成可朗读候选，并给出「是否发声」的判断依据。

    Args:
        raw: LLM 原始回复文本（可含 Markdown）。
        min_len: 最小有效字数，``<=0`` 表示不限制；低于则跳过。
        max_len: 最大有效字数，``<=0`` 表示不限制；高于则跳过（不截断）。

    Returns:
        ``(clean_text, reason)``：
        - ``reason`` 为空串 → 应当发声；
        - ``reason`` 非空 → 跳过原因，``clean_text`` 仍返回清理结果便于日志。
    """
    clean = strip_markdown_for_speech(raw)
    if not clean:
        return "", "清理后没有可朗读的正文（可能只有代码/链接/图片）"
    length = speech_length(clean)
    if length <= 0:
        return clean, "清理后没有可发音字符（可能只有动作标签）"
    if min_len and min_len > 0 and length < min_len:
        return clean, f"正文过短（{length} 字 < min_text_len={min_len}）"
    if max_len and max_len > 0 and length > max_len:
        return clean, f"正文过长（{length} 字 > max_text_len={max_len}），已跳过"
    return clean, ""


def roll_probability(probability: float, rng: random.Random | None = None) -> bool:
    """按概率决定是否发声。

    Args:
        probability: 0.0 ~ 1.0；越界会被裁剪。
        rng: 可注入的随机源（单测用固定种子）。

    Returns:
        True 表示本次应当发声。
    """
    try:
        prob = float(probability)
    except (TypeError, ValueError):
        prob = 1.0
    prob = max(0.0, min(prob, 1.0))
    if prob <= 0.0:
        return False
    if prob >= 1.0:
        return True
    generator = rng or random
    return generator.random() < prob


def inject_system_prompt(
    system_prompt: str,
    directive: str,
    marker: str = PROMPT_INJECTION_MARKER,
) -> str:
    """把导演提示词幂等追加到 system_prompt 末尾。

    幂等策略：``system_prompt`` 中已含 ``marker`` 时原样返回，
    因此不会在多轮请求里重复堆叠，也不会破坏原有人设文本。

    Args:
        system_prompt: 现有系统提示词（可能为空）。
        directive: 要注入的指导词。
        marker: 幂等标记。

    Returns:
        注入后的系统提示词。
    """
    base = system_prompt or ""
    text = (directive or "").strip()
    if not text:
        return base
    if marker and marker in base:
        return base
    block = f"{marker} {text}" if marker else text
    if not base.strip():
        return block
    return f"{base.rstrip()}\n\n{block}"


def normalize_api_endpoint(api_base: str) -> str:
    """把 provider 的 api_base 归一化为 chat/completions 端点。

    AstrBot 原生 MiMo TTS provider 的 ``api_base`` 形如
    ``https://api.xiaomimimo.com/v1``，而本插件使用 OpenAI 兼容的
    ``/chat/completions`` 端点，这里统一补齐。

    Args:
        api_base: provider 配置中的 api_base 或完整端点。

    Returns:
        完整的 chat/completions URL。
    """
    base = (api_base or "").strip()
    if not base:
        return DEFAULT_API_URL
    if base.rstrip("/").endswith("/chat/completions"):
        return base.rstrip("/")
    return base.rstrip("/") + "/chat/completions"


def parse_native_tts_config(data: dict) -> dict[str, str]:
    """从 AstrBot 全局配置数据中提取 MiMo TTS provider 的凭据。

    查找策略：
    1. 优先 ``provider`` 列表（运行时实际生效的配置）；
    2. 回退 ``provider_sources``（服务来源模板）；
    3. 命中条件：``id/type/provider/model`` 任一含 ``mimo``，且
       ``provider_type`` 为空或为 ``text_to_speech``。

    Args:
        data: 已解析的 ``cmd_config.json`` 字典。

    Returns:
        含 ``api_key`` / ``api_base`` / ``model`` / ``voice`` /
        ``style_prompt`` / ``provider_id`` / ``tts_enabled`` /
        ``tts_provider_id`` 的字典；未命中时各值为空串。
    """
    result: dict[str, str] = {
        "api_key": "",
        "api_base": "",
        "model": "",
        "voice": "",
        "style_prompt": "",
        "provider_id": "",
        "tts_enabled": "",
        "tts_provider_id": "",
        "provider_enabled": "",
    }
    if not isinstance(data, dict):
        return result

    tts_settings = data.get("provider_tts_settings") or {}
    if isinstance(tts_settings, dict):
        enabled = tts_settings.get("enable")
        result["tts_enabled"] = "1" if bool(enabled) else "0"
        result["tts_provider_id"] = str(tts_settings.get("provider_id") or "").strip()

    def _iter_mimo(items: Any) -> list[dict]:
        found: list[dict] = []
        if not isinstance(items, (list, tuple)):
            return found
        for item in items:
            if not isinstance(item, dict):
                continue
            blob = " ".join(
                str(item.get(key, "")) for key in ("id", "type", "provider", "model")
            ).lower()
            if "mimo" not in blob:
                continue
            ptype = str(item.get("provider_type") or "").lower().strip()
            if ptype and ptype != "text_to_speech":
                continue
            found.append(item)
        return found

    candidates = _iter_mimo(data.get("provider")) + _iter_mimo(
        data.get("provider_sources")
    )

    for item in candidates:
        if not result["api_key"]:
            raw_key = str(item.get("api_key") or item.get("key") or "").strip()
            # provider_sources 的 key 可能是 "['sk-xxx']" 形式，取第一个引号内值
            if raw_key.startswith("["):
                inner = raw_key.strip("[]").strip(_QUOTES).strip()
                raw_key = inner.split(_QUOTES[0])[0] if inner else ""
            raw_key = raw_key.strip(_QUOTES).strip()
            if raw_key and " " not in raw_key:
                result["api_key"] = raw_key
        if not result["api_base"]:
            result["api_base"] = str(item.get("api_base") or "").strip()
        if not result["model"]:
            result["model"] = str(item.get("model") or "").strip()
        if not result["voice"]:
            result["voice"] = str(item.get("mimo-tts-voice") or "").strip()
        if not result["style_prompt"]:
            result["style_prompt"] = str(
                item.get("mimo-tts-style-prompt") or ""
            ).strip()
        if not result["provider_id"]:
            result["provider_id"] = str(item.get("id") or "").strip()
        if not result["provider_enabled"]:
            enabled = item.get("enable")
            if enabled is not None:
                result["provider_enabled"] = "1" if bool(enabled) else "0"

    return result


def native_config_candidates(explicit_path: str = "") -> list[Path]:
    """列出可能的 AstrBot 全局配置路径（按优先级去重）。

    不写死绝对路径：优先使用 AstrBot 官方的 data 路径工具，
    再根据 ``main.py`` 所在位置向上回溯，最后兜底当前工作目录。

    Args:
        explicit_path: 用户在插件配置中显式指定的配置文件路径。

    Returns:
        去重后的候选路径列表（不保证存在）。
    """
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path))

    try:  # 官方路径工具（在 AstrBot 运行时可用）
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(Path(get_astrbot_data_path()) / "cmd_config.json")
    except Exception:  # noqa: BLE001 - 离线环境允许缺失
        pass

    here = Path(__file__).resolve()
    # <root>/data/plugins/<plugin>/main.py -> data, <root>, ...
    for parent in list(here.parents)[:6]:
        candidates.append(parent / "cmd_config.json")
        candidates.append(parent / "data" / "cmd_config.json")

    candidates.append(Path.cwd() / "data" / "cmd_config.json")

    seen: set[str] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


# --------------------------------------------------------------------------- #
# 数据容器
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TTSParams:
    """一次语音合成的参数集合。"""

    text: str
    style: str = ""
    voice: str = ""
    model: str = ""
    style_instruction: str = ""
    optimize_text_preview: bool = False


class MiMoTTSError(RuntimeError):
    """MiMo TTS 调用或音频处理失败。"""


class SessionStateStore:
    """会话级配置覆盖（音色 / 风格），持久化到插件数据目录。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    def load(self) -> None:
        """从磁盘加载（同步，启动时调用一次）。"""
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(f"[mimotts] 会话状态文件损坏，已忽略: {exc}")
            return
        if isinstance(raw, dict):
            self._data = {
                str(sid): {str(k): str(v) for k, v in item.items()}
                for sid, item in raw.items()
                if isinstance(item, dict)
            }

    def get(self, session_id: str, key: str, default: str = "") -> str:
        """读取会话级配置。"""
        return self._data.get(session_id, {}).get(key, default) or default

    async def set(self, session_id: str, key: str, value: str) -> None:
        """写入并异步落盘。"""
        async with self._lock:
            bucket = self._data.setdefault(session_id, {})
            if value:
                bucket[key] = value
            else:
                bucket.pop(key, None)
            snapshot = json.dumps(self._data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_atomic, snapshot)

    def _write_atomic(self, snapshot: str) -> None:
        """原子写入，避免半截文件。"""
        tmp = self._path.with_suffix(".tmp")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(snapshot, encoding="utf-8")
        tmp.replace(self._path)


# --------------------------------------------------------------------------- #
# MiMo TTS 客户端
# --------------------------------------------------------------------------- #


class MiMoClient:
    """小米 MiMo-V2.5-TTS 非流式调用封装。"""

    def __init__(self, plugin: MiMoTTSPlugin) -> None:
        self._plugin = plugin
        self._http: aiohttp.ClientSession | None = None

    async def get_session(self) -> aiohttp.ClientSession:
        """惰性创建并复用 aiohttp 会话。"""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._plugin.request_timeout)
            )
        return self._http

    async def close(self) -> None:
        """关闭底层会话。"""
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    def _build_messages(self, params: TTSParams) -> list[dict[str, str]]:
        """构造 messages：user=风格指令（不发音），assistant=朗读正文。"""
        model = params.model or MODEL_TTS
        content = compose_assistant_content(
            params.text,
            params.style,
            enable_audio_tags=self._plugin.enable_audio_tags,
        )

        if model == MODEL_VOICEDESIGN:
            # VoiceDesign：user 内容即音色描述，且为必填
            user_prompt = (
                params.style_instruction
                or self._plugin.voice_design_prompt
                or "温婉的年轻女声，语气轻柔自然，语速偏慢。"
            )
            return [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": content},
            ]

        # 内置音色 / 音色克隆：user 放自然语言风格与导演指令（可选）
        instruction = (params.style_instruction or "").strip()
        if not instruction:
            instruction = self._plugin.style_instruction
        return [
            {"role": "user", "content": instruction},
            {"role": "assistant", "content": content},
        ]

    def _build_audio_cfg(self, params: TTSParams) -> dict[str, Any]:
        """构造 audio 参数块（wav 非流式）。"""
        model = params.model or MODEL_TTS
        cfg: dict[str, Any] = {"format": "wav"}

        if model == MODEL_VOICEDESIGN:
            cfg["optimize_text_preview"] = bool(params.optimize_text_preview)
            return cfg

        if model == MODEL_VOICECLONE:
            voice_ref = self._plugin.build_voice_clone_ref()
            if not voice_ref:
                raise MiMoTTSError(
                    "voiceclone 模型需要在插件配置中提供 voice_sample_path（mp3/wav 样本）"
                )
            cfg["voice"] = voice_ref
            return cfg

        cfg["voice"] = params.voice or self._plugin.default_voice or "冰糖"
        return cfg

    async def synthesize(self, params: TTSParams) -> bytes:
        """调用 MiMo 合成语音，返回 WAV 音频字节。

        Args:
            params: 合成参数。

        Returns:
            WAV 音频字节。

        Raises:
            MiMoTTSError: 凭证缺失、网络失败、接口报错或返回体无音频。
        """
        api_key = self._plugin.resolve_api_key()
        if not api_key:
            raise MiMoTTSError(
                "未找到 MiMo API Key：请任选其一 —— "
                "① 在 AstrBot 面板配置原生 MiMo TTS provider（本插件会自动复用）；"
                "② 在插件配置 api_key 填写；"
                f"③ 将 MIMO_API_KEY 写入 {self._plugin.credentials_path} 或环境变量。"
            )

        payload = {
            "model": params.model or MODEL_TTS,
            "messages": self._build_messages(params),
            "audio": self._build_audio_cfg(params),
            "stream": False,
        }
        headers = {
            # 官方 curl 示例使用 api-key；同时附带 Bearer 以兼容 OpenAI 风格网关
            "api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        session = await self.get_session()
        url = self._plugin.resolve_api_url()
        if self._plugin.debug:
            logger.info(
                f"[mimotts] 请求 {url} model={payload['model']} "
                f"audio={payload['audio']} assistant={payload['messages'][-1]['content'][:80]!r}"
            )

        try:
            async with session.post(url, json=payload, headers=headers) as resp:
                body = await resp.text()
                status = resp.status
        except TimeoutError as exc:
            raise MiMoTTSError(
                f"请求 MiMo 超时（{self._plugin.request_timeout}s）"
            ) from exc
        except aiohttp.ClientError as exc:
            raise MiMoTTSError(f"请求 MiMo 失败：{exc}") from exc

        if status != 200:
            raise MiMoTTSError(f"MiMo 返回 HTTP {status}：{body[:300]}")

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise MiMoTTSError(f"MiMo 返回非 JSON 内容：{body[:200]}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise MiMoTTSError(f"MiMo 接口报错：{message}")

        choices = data.get("choices") or []
        if not choices:
            raise MiMoTTSError(f"MiMo 返回缺少 choices：{body[:200]}")
        message = choices[0].get("message") or {}
        audio = message.get("audio") or {}
        b64 = audio.get("data") if isinstance(audio, dict) else None
        if not b64:
            raise MiMoTTSError(f"MiMo 返回缺少 audio.data：{body[:200]}")

        try:
            raw = base64.b64decode(b64)
        except (ValueError, TypeError) as exc:
            raise MiMoTTSError("audio.data base64 解码失败") from exc
        if len(raw) < 128:
            raise MiMoTTSError(f"MiMo 返回音频过短（{len(raw)} bytes）")

        logger.info(
            f"[mimotts] 合成成功 model={payload['model']} size={len(raw)} bytes"
        )
        return raw


# --------------------------------------------------------------------------- #
# 插件主类
# --------------------------------------------------------------------------- #


class MiMoTTSPlugin(Star):
    """小米 MiMo TTS —— 微信 / NapCat QQ 双平台原生语音条。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config: dict = config or {}

        # ---- 配置读取（全部带安全默认值）
        cfg = self.config
        self.enable: bool = self._as_bool(cfg.get("enable", True), True)
        self.api_url: str = str(cfg.get("api_url") or DEFAULT_API_URL).strip()
        self.credentials_path: str = str(
            cfg.get("credentials_path") or DEFAULT_CREDENTIALS_PATH
        ).strip()
        self.model: str = str(cfg.get("model") or MODEL_TTS).strip()
        self.default_voice: str = str(cfg.get("default_voice") or "冰糖").strip()
        self.voice_design_prompt: str = str(
            cfg.get("voice_design_prompt") or ""
        ).strip()
        self.voice_sample_path: str = str(cfg.get("voice_sample_path") or "").strip()
        self.voice_sample_format: str = (
            str(cfg.get("voice_sample_format") or "mp3").strip().lower()
        )
        self.style_prefix: str = str(cfg.get("style_prefix") or "").strip()
        self.style_instruction: str = str(cfg.get("style_instruction") or "").strip()
        self.enable_audio_tags: bool = self._as_bool(
            cfg.get("enable_audio_tags", True), True
        )
        self.target_platforms: list[str] = self._as_platform_list(
            cfg.get("target_platforms")
        )
        self.weixin_voice_mode: str = (
            str(cfg.get("weixin_voice_mode") or "auto").strip().lower()
        )
        self.weixin_voice_upload_type: int = self._as_int(
            cfg.get("weixin_voice_upload_type", 3), 3
        )
        self.qq_record_format: str = (
            str(cfg.get("qq_record_format") or "auto").strip().lower()
        )
        self.send_text_caption: bool = self._as_bool(
            cfg.get("send_text_caption", False), False
        )
        self.max_text_length: int = self._as_int(cfg.get("max_text_length", 300), 300)
        self.request_timeout: int = self._as_int(cfg.get("request_timeout", 90), 90)
        self.enable_llm_tool: bool = self._as_bool(
            cfg.get("enable_llm_tool", True), True
        )
        self.temp_ttl_seconds: int = self._as_int(cfg.get("temp_ttl_seconds", 900), 900)
        self.debug: bool = self._as_bool(cfg.get("debug", False), False)

        # ---- v1.1.0 自动语音架构配置
        self.enable_auto_hook: bool = self._as_bool(
            cfg.get("enable_auto_hook", True), True
        )
        self.auto_speak_probability: float = self._as_float(
            cfg.get("auto_speak_probability", 0.6), 0.6
        )
        self.send_mode: str = (
            str(cfg.get("send_mode") or SEND_MODE_WITH_TEXT).strip().lower()
        )
        if self.send_mode not in SEND_MODES:
            logger.warning(
                f"[mimotts] 未知 send_mode={self.send_mode!r}，已回退 {SEND_MODE_WITH_TEXT}"
            )
            self.send_mode = SEND_MODE_WITH_TEXT
        self.default_style: str = str(cfg.get("default_style") or "").strip()
        self.enable_prompt_injection: bool = self._as_bool(
            cfg.get("enable_prompt_injection", True), True
        )
        self.prompt_injection_text: str = str(
            cfg.get("prompt_injection_text") or ""
        ).strip()
        self.min_text_len: int = self._as_int(cfg.get("min_text_len", 2), 2)
        self.max_text_len: int = self._as_int(cfg.get("max_text_len", 200), 200)
        self.auto_hook_skip_if_native_tts: bool = self._as_bool(
            cfg.get("auto_hook_skip_if_native_tts", True), True
        )
        self.native_config_path: str = str(
            cfg.get("native_config_path") or ""
        ).strip()

        self._api_key_override: str = str(cfg.get("api_key") or "").strip()
        self._optimize_text_preview = self._as_bool(
            cfg.get("voice_design_optimize_text", False), False
        )

        # 原生 TTS 配置缓存（60s，避免每次合成/注入都读盘）
        self._native_tts_cache: dict[str, str] | None = None
        self._native_tts_ts: float = 0.0
        # 概率判定随机源（单测可注入固定种子）
        self._rng: random.Random = random.Random()
        # 原生 TTS 冲突告警节流
        self._native_tts_warn_ts: float = 0.0

        # ---- 目录与状态
        self.data_dir: Path = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.cache_dir: Path = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.state = SessionStateStore(self.data_dir / "session_state.json")
        self.state.load()

        self.client = MiMoClient(self)
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._ffmpeg_path: str | None = shutil.which("ffmpeg")

        logger.info(
            f"[mimotts] 插件加载：model={self.model} voice={self.default_voice} "
            f"ffmpeg={'可用' if self._ffmpeg_path else '缺失'} "
            f"platforms={self.target_platforms or '全部'}"
        )
        logger.info(
            "[mimotts] 自动发声："
            f"{'开启' if self.enable_auto_hook else '关闭'} "
            f"prob={self.auto_speak_probability:.2f} mode={self.send_mode} "
            f"len=[{self.min_text_len}, {self.max_text_len}] "
            f"prompt注入={'开启' if self.enable_prompt_injection else '关闭'}"
        )

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        """插件启用时调用：清扫过期临时文件并自检依赖。"""
        removed = await self.cleanup_temp_files(force_all=True)
        if removed:
            logger.info(f"[mimotts] 启动清理临时音频 {removed} 个")
        if not self._ffmpeg_path:
            logger.warning(
                "[mimotts] 未检测到 ffmpeg，将直接使用 MiMo 返回的 24kHz WAV；"
                "部分平台可能无法播放，建议安装 ffmpeg。"
            )
        if not self.resolve_api_key():
            logger.warning(
                "[mimotts] 未解析到 MiMo API Key：请配置 AstrBot 原生 MiMo TTS provider、"
                f"插件 api_key、环境变量 MIMO_API_KEY 或 {self.credentials_path}"
            )
        else:
            logger.info(
                f"[mimotts] API Key 来源：{self.resolve_api_key_source()}；"
                f"端点：{self.resolve_api_url()}"
            )

        # 原生 TTS 冲突提示（避免双重扣费 / 重合发声）
        native = self.native_tts_info(force=True)
        if native.get("tts_enabled") == "1":
            if self.send_mode == SEND_MODE_WITH_TEXT and self.auto_hook_skip_if_native_tts:
                logger.warning(
                    "[mimotts] 检测到 AstrBot 原生 TTS 已启用（provider="
                    f"{native.get('provider_id') or native.get('tts_provider_id')}）："
                    "「文字+语音」模式下自动发声将让位以避免双重扣费。"
                    "请按 README《如何关闭 AstrBot 原生 TTS》关闭原生 TTS。"
                )
            else:
                logger.warning(
                    "[mimotts] 检测到 AstrBot 原生 TTS 已启用；"
                    "voice_only 模式本身不会与原生 TTS 重合，但建议关闭原生 TTS 以统一音色。"
                )

    async def terminate(self) -> None:
        """插件停用时调用：关闭网络会话与后台清理任务。"""
        await self.client.close()
        for task in list(self._cleanup_tasks):
            task.cancel()
        self._cleanup_tasks.clear()
        stale = await self.cleanup_temp_files(force_all=True)
        if stale:
            logger.info(f"[mimotts] 退出清理临时音频 {stale} 个")

    # ---------------- 配置辅助 ----------------

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        """宽松布尔解析。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    @staticmethod
    def _as_int(value: Any, default: int) -> int:
        """宽松整数解析。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_float(value: Any, default: float) -> float:
        """宽松浮点解析（并裁剪到 0.0~1.0 以外由调用方决定）。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_platform_list(value: Any) -> list[str]:
        """把配置里的平台列表归一化成小写平台名列表。"""
        if value is None:
            return []
        if isinstance(value, str):
            items = re.split(r"[,\s]+", value.strip())
        elif isinstance(value, (list, tuple, set)):
            items = [str(item) for item in value]
        else:
            items = [str(value)]
        return [item.strip().lower() for item in items if item and item.strip()]

    def native_tts_info(self, *, force: bool = False) -> dict[str, str]:
        """读取并缓存 AstrBot 原生 MiMo TTS provider 配置。

        缓存 60 秒，避免每次消息都解析 60KB+ 的 ``cmd_config.json``；
        面板改配置后最多 1 分钟自动生效（``/mimo状态`` 会强制刷新）。

        Args:
            force: 忽略缓存，强制重新读盘。

        Returns:
            同 :func:`parse_native_tts_config`，额外含 ``config_path``。
        """
        now = time.time()
        if (
            not force
            and self._native_tts_cache is not None
            and now - self._native_tts_ts < 60
        ):
            return self._native_tts_cache

        info: dict[str, str] = {}
        # 显式指定路径时只认该文件（便于部署与单测隔离），否则自动探测
        candidates = (
            [Path(self.native_config_path)]
            if self.native_config_path
            else native_config_candidates()
        )
        for path in candidates:
            try:
                if not path.is_file():
                    continue
                # cmd_config.json 可能带 UTF-8 BOM
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                logger.debug(f"[mimotts] 读取原生配置失败 {path}: {exc}")
                continue
            parsed = parse_native_tts_config(raw)
            if parsed.get("api_key") or parsed.get("tts_provider_id"):
                parsed["config_path"] = str(path)
                info = parsed
                break

        self._native_tts_cache = info
        self._native_tts_ts = now
        return info

    def native_tts_enabled(self) -> bool:
        """原生 TTS 当前是否启用（用于避免双重扣费）。

        需要同时满足：``provider_tts_settings.enable`` 为真，
        且 MiMo provider 自身未被禁用（``enable=false``）。
        """
        info = self.native_tts_info()
        if info.get("tts_enabled") != "1":
            return False
        return info.get("provider_enabled") != "0"

    def resolve_api_key(self) -> str:
        """解析 MiMo API Key（凭据优先级，开箱即用）。

        优先级：
        1. AstrBot 全局配置 ``cmd_config.json`` 中原生 MiMo TTS provider 的 ``api_key``
           （用户在面板里已填过的 Key，直接复用，免二次配置）；
        2. 插件自身配置 ``api_key``；
        3. 环境变量 ``MIMO_API_KEY``；
        4. 凭证文件 ``credentials_path``（默认 ``/data/apps/credentials/mimo.env``）。

        Returns:
            解析到的 Key；全部缺失时返回空字符串。
        """
        native_key = self.native_tts_info().get("api_key", "").strip()
        if native_key:
            return native_key
        if self._api_key_override:
            return self._api_key_override
        env_key = os.environ.get("MIMO_API_KEY", "").strip()
        if env_key:
            return env_key
        return parse_env_file(self.credentials_path)

    def resolve_api_key_source(self) -> str:
        """描述当前 Key 的来源（供 /mimo状态 展示）。"""
        native = self.native_tts_info()
        if native.get("api_key"):
            return f"AstrBot 原生配置（{native.get('provider_id') or 'mimo'}）"
        if self._api_key_override:
            return "插件配置 api_key"
        if os.environ.get("MIMO_API_KEY", "").strip():
            return "环境变量 MIMO_API_KEY"
        if parse_env_file(self.credentials_path):
            return f"凭证文件 {self.credentials_path}"
        return "未配置"

    def resolve_api_url(self) -> str:
        """解析实际请求端点：插件显式配置 > 原生 provider api_base > 默认值。"""
        configured = (self.api_url or "").strip()
        if configured and configured != DEFAULT_API_URL:
            return normalize_api_endpoint(configured)
        native_base = self.native_tts_info().get("api_base", "").strip()
        if native_base:
            return normalize_api_endpoint(native_base)
        return normalize_api_endpoint(configured or DEFAULT_API_URL)

    def build_voice_clone_ref(self) -> str:
        """构造 voiceclone 所需的 `data:audio/xxx;base64,...` 音色参考。"""
        path = Path(self.voice_sample_path) if self.voice_sample_path else None
        if path is None or not path.is_file():
            return ""
        suffix = path.suffix.lower().lstrip(".") or self.voice_sample_format
        mime = {
            "mp3": "audio/mpeg",
            "mpeg": "audio/mpeg",
            "wav": "audio/wav",
        }.get(suffix, "audio/mpeg")
        try:
            data = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as exc:
            logger.error(f"[mimotts] 读取音色样本失败 {path}: {exc}")
            return ""
        if len(data) > 10 * 1024 * 1024:
            logger.error("[mimotts] 音色样本 base64 超过 10MB，MiMo 会拒绝")
            return ""
        return f"data:{mime};base64,{data}"

    # ---------------- 文本与参数整理 ----------------

    def prepare_params(
        self,
        raw_text: str,
        *,
        session_id: str = "",
        voice: str = "",
        style: str = "",
        model: str = "",
    ) -> TTSParams:
        """把用户输入整理成合规的合成参数（含 <style> 转换与长度限制）。"""
        extracted_style, body = extract_style_and_body(raw_text)

        # 风格优先级：显式参数 > 文本内 <style> > 会话设置 > 插件默认
        effective_style = (
            (style or "").strip()
            or extracted_style
            or (self.state.get(session_id, "style") if session_id else "")
            or self.style_prefix
            or self.default_style
        )

        body, truncated = clamp_text(body, self.max_text_length)
        if truncated:
            logger.info(
                f"[mimotts] 文本超长已截断至 {self.max_text_length} 字（原 {len(raw_text)} 字）"
            )

        effective_voice = (
            (voice or "").strip()
            or (self.state.get(session_id, "voice") if session_id else "")
            or self.default_voice
        )

        return TTSParams(
            text=body,
            style=effective_style,
            voice=effective_voice,
            model=(model or self.model),
            style_instruction=self.style_instruction,
            optimize_text_preview=self._optimize_text_preview,
        )

    # ---------------- 音频处理 ----------------

    async def _run_ffmpeg(self, src: Path, dst: Path, extra_args: list[str]) -> Path:
        """调用 ffmpeg 转码，带超时与错误检查。"""
        if not self._ffmpeg_path:
            raise MiMoTTSError("ffmpeg 不可用")
        proc = await asyncio.create_subprocess_exec(
            self._ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            *extra_args,
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise MiMoTTSError("ffmpeg 转码超时") from exc

        if proc.returncode != 0:
            message = (stderr or b"").decode("utf-8", "ignore").strip()[:300]
            raise MiMoTTSError(f"ffmpeg 转码失败（code={proc.returncode}）：{message}")
        if not dst.is_file() or dst.stat().st_size == 0:
            raise MiMoTTSError("ffmpeg 未生成有效输出文件")
        return dst

    async def normalize_wav(self, src: Path, token: str) -> Path:
        """统一音频为 24kHz / 单声道 / 16bit PCM WAV。"""
        dst = src.with_name(f"mimo_{token}_24k.wav")
        try:
            return await self._run_ffmpeg(
                src,
                dst,
                ["-vn", "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1"],
            )
        except MiMoTTSError as exc:
            logger.warning(f"[mimotts] WAV 标准化失败，回退原始音频：{exc}")
            return src

    async def to_mp3(self, src: Path, token: str) -> Path:
        """转 mp3（微信文件降级播放用）。"""
        dst = src.with_name(f"mimo_{token}.mp3")
        try:
            return await self._run_ffmpeg(
                src,
                dst,
                [
                    "-vn",
                    "-acodec",
                    "libmp3lame",
                    "-ar",
                    "24000",
                    "-ac",
                    "1",
                    "-q:a",
                    "5",
                ],
            )
        except MiMoTTSError as exc:
            logger.warning(f"[mimotts] 转 MP3 失败：{exc}")
            return src

    async def to_tencent_silk(self, src: Path, token: str) -> Path | None:
        """尝试把 WAV 编码为 Tencent SILK（需要 pysilk）。"""
        dst = src.with_name(f"mimo_{token}.silk")
        try:
            from astrbot.core.utils.tencent_record_helper import (  # noqa: WPS433
                wav_to_tencent_silk,
            )
        except ImportError:
            logger.debug("[mimotts] 未安装 pysilk，跳过 SILK 编码")
            return None

        try:
            await wav_to_tencent_silk(str(src), str(dst))
        except Exception as exc:  # noqa: BLE001 - pysilk 异常类型不稳定，统一兜底
            logger.warning(f"[mimotts] SILK 编码失败，回退 WAV：{exc}")
            return None
        if dst.is_file() and dst.stat().st_size > 0:
            return dst
        return None

    # ---------------- 临时文件清理 ----------------

    async def cleanup_temp_files(self, *, force_all: bool = False) -> int:
        """清理缓存目录中的过期音频文件。

        Args:
            force_all: True 时忽略 TTL 全部清理。

        Returns:
            实际删除的文件数量。
        """
        deadline = time.time() - max(self.temp_ttl_seconds, 60)
        removed = 0

        def _scan() -> int:
            count = 0
            for item in self.cache_dir.glob("mimo_*"):
                try:
                    if not item.is_file():
                        continue
                    if force_all or item.stat().st_mtime < deadline:
                        item.unlink(missing_ok=True)
                        count += 1
                except OSError as exc:
                    logger.debug(f"[mimotts] 清理文件失败 {item}: {exc}")
            return count

        try:
            removed = await asyncio.to_thread(_scan)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响主流程
            logger.debug(f"[mimotts] 清理任务异常：{exc}")
        return removed

    def schedule_cleanup(self, paths: list[Path], delay: int = 60) -> None:
        """延迟清理指定文件（发送完成后调用）。"""
        targets = [p for p in paths if p]

        async def _worker() -> None:
            await asyncio.sleep(max(delay, 10))
            for path in targets:
                try:
                    await asyncio.to_thread(path.unlink, True)
                except OSError as exc:
                    logger.debug(f"[mimotts] 删除临时音频失败 {path}: {exc}")

        task = asyncio.create_task(_worker())
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)

    # ---------------- 自动钩子：组件构造 ----------------

    def platform_allowed(self, event: AstrMessageEvent) -> bool:
        """当前平台是否在 target_platforms 白名单内（空表示全部）。"""
        platform = (event.get_platform_name() or "").lower()
        return not self.target_platforms or platform in self.target_platforms

    async def build_voice_component(
        self,
        event: AstrMessageEvent,
        wav_bytes: bytes,
    ) -> tuple[Any | None, str]:
        """把 MiMo WAV 字节转换为可拼进消息链的语音组件（不直接发送）。

        与原 ``deliver_voice`` 的区别：本方法只负责「造组件」，由调用方决定
        追加到结果链还是替换结果链，从而支持 `on_decorating_result` 钩子。

        平台策略：
        - aiocqhttp（NapCat/OneBot v11）：优先编码 Tencent SILK，发 Record 语音条；
        - weixin_oc：出站不支持 Record，降级为 mp3 文件（File）；
        - 其它平台：发 Record，由框架自行转换。

        Args:
            event: 消息事件（用于判断平台）。
            wav_bytes: MiMo 返回的 WAV 音频字节。

        Returns:
            ``(component, note)``；失败时 component 为 None，note 为原因。
        """
        platform = (event.get_platform_name() or "").lower()
        token = uuid.uuid4().hex[:12]
        raw_path = self.cache_dir / f"mimo_{token}_raw.wav"
        try:
            await asyncio.to_thread(raw_path.write_bytes, wav_bytes)
        except OSError as exc:
            return None, f"写入临时音频失败：{exc}"

        created: list[Path] = [raw_path]
        try:
            wav_path = await self.normalize_wav(raw_path, token)
            if wav_path != raw_path:
                created.append(wav_path)

            if platform == "weixin_oc":
                mp3_path = await self.to_mp3(wav_path, token)
                if mp3_path != wav_path:
                    created.append(mp3_path)
                component = File(name=mp3_path.name, file=str(mp3_path))
                note = "微信音频文件（原生语音条不支持，已降级）"
            else:
                audio_path = wav_path
                if platform == "aiocqhttp" and self.qq_record_format in (
                    "auto",
                    "silk",
                ):
                    silk_path = await self.to_tencent_silk(wav_path, token)
                    if silk_path is not None:
                        audio_path = silk_path
                        created.append(silk_path)
                        logger.debug("[mimotts] 自动发声：已编码 SILK")
                component = Record.fromFileSystem(str(audio_path))
                note = "语音条" if platform == "aiocqhttp" else f"语音段({platform})"
        except Exception as exc:  # noqa: BLE001 - 组件构造失败不应打断回复
            logger.error(f"[mimotts] 构造语音组件失败：{exc}", exc_info=True)
            for path in created:
                try:
                    await asyncio.to_thread(path.unlink, True)
                except OSError:
                    pass
            return None, f"构造语音消息失败：{exc}"

        # 给框架留足发送时间后再清理临时文件
        self.schedule_cleanup(created, delay=120)
        return component, note

    # ---------------- 多端语音发送 ----------------

    async def _send_record(
        self, event: AstrMessageEvent, wav_path: Path, token: str
    ) -> Path:
        """以语音段（Record）发送，返回实际发送的音频路径。

        NapCat / OneBot v11 会收到 base64 的 record 段并自行转 silk，
        因此这里只需保证音频可解码（24kHz mono WAV 即可）。
        """
        path = wav_path
        platform = (event.get_platform_name() or "").lower()
        if platform == "aiocqhttp" and self.qq_record_format in ("auto", "silk"):
            silk_path = await self.to_tencent_silk(wav_path, token)
            if silk_path is not None:
                path = silk_path
                logger.info("[mimotts] 已编码 SILK，发送 QQ 语音段")
        component = Record.fromFileSystem(str(path))
        await event.send(MessageChain(chain=[component]))
        return path

    async def _send_file(
        self, event: AstrMessageEvent, wav_path: Path, token: str
    ) -> Path:
        """以音频文件形式发送（微信等不支持语音条时的降级方案）。"""
        path = await self.to_mp3(wav_path, token)
        component = File(name=path.name, file=str(path))
        await event.send(MessageChain(chain=[component]))
        return path

    async def _try_weixin_native_voice(
        self, event: AstrMessageEvent, audio_path: Path
    ) -> bool:
        """尝试通过 weixin_oc 适配器私有通道发送原生语音条（实验性）。

        AstrBot 的 weixin_oc 出站只实现了 Image / Video / File，未实现 voice_item；
        这里复用其媒体上传能力，把上传结果重组成 `voice_item` 直接投递。
        协议细节未在官方文档公开，因此任何失败都会被上层自动降级处理。

        Returns:
            True 表示语音条投递成功。
        """
        adapter = getattr(event, "platform", None)
        prepare = getattr(adapter, "_prepare_media_item", None)
        send_items = getattr(adapter, "_send_items_to_session", None)
        if not callable(prepare) or not callable(send_items):
            logger.debug("[mimotts] 微信适配器不支持原生语音条通道，跳过")
            return False

        voice_type = int(getattr(adapter, "VOICE_ITEM_TYPE", 3))
        upload_type = int(self.weixin_voice_upload_type)
        user_id = event.get_session_id() or event.get_sender_id()
        if not user_id:
            logger.debug("[mimotts] 微信语音条缺少 user_id，跳过")
            return False

        try:
            item = await prepare(
                user_id, audio_path, upload_type, voice_type, audio_path.name
            )
            if not isinstance(item, dict):
                return False
            # 适配器对非 image/video 一律返回 file_item，取其 media 载荷重组 voice_item
            container = item.get("file_item") or item.get("voice_item") or {}
            media = container.get("media") if isinstance(container, dict) else None
            if not media:
                logger.debug(f"[mimotts] 微信媒体上传未返回 media 载荷：{item}")
                return False
            voice_item = {"type": voice_type, "voice_item": {"media": media}}
            ok = await send_items(user_id, [voice_item])
            if ok:
                logger.info("[mimotts] 微信原生语音条发送成功（实验通道）")
            else:
                logger.info("[mimotts] 微信原生语音条发送被拒绝，将降级为音频文件")
            return bool(ok)
        except Exception as exc:  # noqa: BLE001 - 实验通道任何异常都降级，不中断对话
            logger.warning(f"[mimotts] 微信原生语音条尝试失败（将降级）：{exc}")
            return False

    async def deliver_voice(
        self, event: AstrMessageEvent, wav_bytes: bytes, caption: str = ""
    ) -> tuple[bool, str]:
        """把音频字节投递到当前会话（自动适配平台）。

        Args:
            event: 消息事件。
            wav_bytes: MiMo 返回的 WAV 字节。
            caption: 可选附带的文字说明。

        Returns:
            (是否成功, 失败原因或成功说明)
        """
        platform = (event.get_platform_name() or "").lower()
        if self.target_platforms and platform not in self.target_platforms:
            return False, f"平台 {platform or '未知'} 未在 target_platforms 中启用"

        token = uuid.uuid4().hex[:12]
        raw_path = self.cache_dir / f"mimo_{token}_raw.wav"
        try:
            await asyncio.to_thread(raw_path.write_bytes, wav_bytes)
        except OSError as exc:
            return False, f"写入临时音频失败：{exc}"

        created: list[Path] = [raw_path]
        wav_path = await self.normalize_wav(raw_path, token)
        if wav_path != raw_path:
            created.append(wav_path)

        sent_path: Path | None = None
        note = ""
        try:
            if caption and self.send_text_caption:
                await event.send(MessageChain(chain=[Plain(caption)]))

            if platform == "weixin_oc":
                mode = self.weixin_voice_mode or "auto"
                if mode in ("auto", "native_voice"):
                    # 微信语音条需要 silk/amr；先尝试转 silk，失败直接用 wav
                    voice_src = await self.to_tencent_silk(wav_path, token) or wav_path
                    if voice_src != wav_path:
                        created.append(voice_src)
                    ok = await self._try_weixin_native_voice(event, voice_src)
                    if ok:
                        sent_path = voice_src
                        note = "微信语音条"
                    elif mode == "native_voice":
                        return (
                            False,
                            "微信原生语音条发送失败（可改用 weixin_voice_mode=auto 降级文件）",
                        )
                if sent_path is None:
                    sent_path = await self._send_file(event, wav_path, token)
                    note = "音频文件（微信暂不支持原生语音条）"
            else:
                sent_path = await self._send_record(event, wav_path, token)
                note = "语音条" if platform == "aiocqhttp" else f"语音段({platform})"
        except Exception as exc:  # noqa: BLE001 - 发送失败必须转成用户可读提示
            logger.error(
                f"[mimotts] 音频发送失败 platform={platform}: {exc}", exc_info=True
            )
            return False, f"音频发送失败：{exc}"
        finally:
            if sent_path is not None and sent_path not in created:
                created.append(sent_path)
            self.schedule_cleanup(created)

        return True, note

    # ---------------- 核心流程 ----------------

    async def handle_tts(
        self,
        event: AstrMessageEvent,
        raw_text: str,
        *,
        voice: str = "",
        style: str = "",
    ) -> tuple[bool, str]:
        """合成并发送语音（命令与 LLM 工具共用）。"""
        if not self.enable:
            return False, "MiMo TTS 插件已被配置禁用（enable=false）"

        text = (raw_text or "").strip()
        if not text:
            return False, "朗读文本为空"

        params = self.prepare_params(
            text,
            session_id=event.get_session_id(),
            voice=voice,
            style=style,
        )
        if not params.text:
            return False, "清理后没有可朗读的正文（可能只包含代码/链接）"

        try:
            wav_bytes = await self.client.synthesize(params)
        except MiMoTTSError as exc:
            logger.warning(f"[mimotts] 合成失败：{exc}")
            return False, str(exc)

        return await self.deliver_voice(event, wav_bytes, caption=params.text)

    # ---------------- 事件钩子：隐式 System Prompt 注入 ----------------

    @filter.on_llm_request()
    async def hook_inject_director_prompt(
        self, event: AstrMessageEvent, req: Any
    ) -> None:
        """在 LLM 请求前注入 MiMo 标签指导词（幂等，不破坏原人设）。

        只有「插件启用 + 自动发声开启 + 注入开关开启 + 平台命中」时才注入，
        避免用户只使用指令功能时被无关提示词干扰。

        Args:
            event: 消息事件。
            req: ``ProviderRequest``，其 ``system_prompt`` 可原地修改。
        """
        try:
            if not (
                self.enable and self.enable_auto_hook and self.enable_prompt_injection
            ):
                return
            if not self.platform_allowed(event):
                return

            directive = self.prompt_injection_text or DEFAULT_PROMPT_INJECTION
            current = str(getattr(req, "system_prompt", "") or "")
            updated = inject_system_prompt(current, directive)
            if updated != current:
                req.system_prompt = updated
                logger.debug(
                    f"[mimotts] 已注入导演提示词（新增 {len(updated) - len(current)} 字符）"
                )
        except Exception as exc:  # noqa: BLE001 - 注入失败绝不能影响正常对话
            logger.warning(f"[mimotts] 提示词注入失败（已忽略）：{exc}")

    # ---------------- 事件钩子：LLM 回复拦截与自动发声 ----------------

    def _warn_throttled(self, message: str) -> None:
        """按固定间隔节流告警，避免逐条消息刷屏。"""
        now = time.time()
        if now - self._native_tts_warn_ts < NATIVE_TTS_WARN_INTERVAL:
            return
        self._native_tts_warn_ts = now
        logger.warning(message)

    async def _maybe_auto_speak(self, event: AstrMessageEvent) -> None:
        """自动发声主流程（供 ``on_decorating_result`` 钩子调用）。

        闸门顺序：启用 → 平台 → LLM 结果 → 非流式 → 未重复发声 → 无既有语音
        → Markdown 清理 → 字数区间 → 概率 → 原生 TTS 冲突 → Key → 合成。
        任何一步不通过都保持原有纯文本回复，绝不影响正常聊天。
        """
        if not (self.enable and self.enable_auto_hook):
            return
        if not self.platform_allowed(event):
            logger.debug(
                f"[mimotts] 自动发声跳过：平台 {event.get_platform_name()} 未启用"
            )
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        # 只处理 LLM 最终回复：指令输出（GENERAL_RESULT）不自动发声
        if not result.is_llm_result():
            logger.debug("[mimotts] 自动发声跳过：非 LLM 结果")
            return
        if result.result_content_type in (
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        ):
            logger.debug("[mimotts] 自动发声跳过：流式输出暂不支持")
            return
        # mimo_speak 工具已在本轮发过语音 → 不重复
        if event.get_extra(HOOK_SPOKEN_EXTRA_KEY):
            logger.debug("[mimotts] 自动发声跳过：本轮已由 mimo_speak 工具发声")
            return
        # 结果链已有语音段（原生 TTS / 其它插件）→ 不重复
        if any(isinstance(comp, Record) for comp in result.chain):
            logger.debug("[mimotts] 自动发声跳过：结果链已含语音段")
            return

        raw_text = "".join(
            str(getattr(comp, "text", "") or "")
            for comp in result.chain
            if isinstance(comp, Plain)
        )
        clean, reason = prepare_speech_candidate(
            raw_text,
            min_len=self.min_text_len,
            max_len=self.max_text_len,
        )
        if reason:
            logger.debug(f"[mimotts] 自动发声跳过：{reason} | 文本={clean[:40]!r}")
            return

        if not roll_probability(self.auto_speak_probability, self._rng):
            logger.debug(
                f"[mimotts] 自动发声跳过：概率未命中"
                f"（p={self.auto_speak_probability:.2f}）"
            )
            return

        # 「文字+语音」模式下，原生 TTS 会在本钩子之后再次处理 Plain 文本，
        # 造成同一句话合成两次（双重扣费 + 重合发声），因此主动让位。
        if (
            self.send_mode == SEND_MODE_WITH_TEXT
            and self.auto_hook_skip_if_native_tts
            and self.native_tts_enabled()
        ):
            self._warn_throttled(
                "[mimotts] 检测到 AstrBot 原生 TTS 仍启用：为避免双重扣费/重合发声，"
                "本次自动发声已让位。请按 README《如何关闭 AstrBot 原生 TTS》关闭原生 TTS，"
                "或将 send_mode 改为 voice_only。"
            )
            return

        if not self.resolve_api_key():
            self._warn_throttled(
                "[mimotts] 未解析到 MiMo API Key，自动发声不可用："
                "请在 AstrBot 面板配置原生 MiMo TTS provider，或在插件配置 api_key。"
            )
            return

        params = self.prepare_params(clean, session_id=event.get_session_id())
        if not params.text:
            logger.debug("[mimotts] 自动发声跳过：参数整理后无正文")
            return

        try:
            wav_bytes = await self.client.synthesize(params)
        except MiMoTTSError as exc:
            logger.warning(f"[mimotts] 自动发声合成失败，保持纯文本：{exc}")
            return

        component, note = await self.build_voice_component(event, wav_bytes)
        if component is None:
            logger.warning(f"[mimotts] 自动发声失败，保持纯文本：{note}")
            return

        if self.send_mode == SEND_MODE_VOICE_ONLY:
            # 纯语音：移除文字段，保留图片/引用等非文本组件，避免丢内容
            kept = [comp for comp in result.chain if not isinstance(comp, Plain)]
            result.chain = kept + [component]
            mode_note = "纯语音（已移除文字）"
        else:
            result.chain.append(component)
            mode_note = "文字+语音"

        logger.info(
            f"[mimotts] 自动发声成功：{note} / {mode_note} / "
            f"{speech_length(clean)} 字 / 音色={params.voice}"
        )

    @filter.on_decorating_result()
    async def hook_auto_speak_on_decorating(self, event: AstrMessageEvent) -> None:
        """LLM 回复发送前拦截：命中条件时自动合成语音并附加到消息链。

        对应 AstrBot 事件钩子 ``OnDecoratingResultEvent``，此时消息尚未发出，
        可以安全地改写 ``event.get_result().chain``。
        """
        try:
            await self._maybe_auto_speak(event)
        except Exception as exc:  # noqa: BLE001 - 钩子异常不能吞掉正常回复
            logger.error(
                f"[mimotts] 自动发声钩子异常（已忽略，不影响回复）：{exc}",
                exc_info=True,
            )

    @staticmethod
    def extract_quoted_text(event: AstrMessageEvent) -> str:
        """提取引用消息（回复）里的纯文本。"""
        try:
            chain = getattr(getattr(event, "message_obj", None), "message", None) or []
        except Exception:  # noqa: BLE001 - 事件结构异常时按无引用处理
            return ""
        from astrbot.api.message_components import Reply  # 局部导入，减少顶部依赖

        for comp in chain:
            if isinstance(comp, Reply):
                if comp.message_str:
                    return str(comp.message_str)
                parts: list[str] = []
                for sub in comp.chain or []:
                    if isinstance(sub, Plain) and sub.text:
                        parts.append(str(sub.text))
                return "".join(parts).strip()
        return ""

    @staticmethod
    def parse_command_args(event: AstrMessageEvent, names: tuple[str, ...]) -> str:
        """从原始消息中取命令词之后的完整参数（保留空格）。

        框架对非 GreedyStr 参数只会传第一个词，这里直接解析原始消息，
        以便支持 `/mimo风格 温柔 撒娇` 这类多词参数。

        Args:
            event: 消息事件。
            names: 命令词候选（含别名）。

        Returns:
            命令词之后的参数文本；找不到命令词时返回空串。
        """
        message = (getattr(event, "message_str", "") or "").strip()
        if not message:
            return ""
        lowered = message.lower()
        for name in names:
            idx = lowered.find(name.lower())
            if idx >= 0:
                return message[idx + len(name) :].strip()
        return ""

    # ---------------- 指令 ----------------

    @filter.command("mimo", alias={"mimo语音", "mimo说话"})
    async def cmd_mimo(self, event: AstrMessageEvent, text: GreedyStr):
        """合成语音：/mimo <文本>

        文本支持官方语法：
        - `<style>温柔 撒娇</style>你好呀` 会自动转换为 `(温柔 撒娇)你好呀`
        - 可直接写 `(温柔)你好呀[轻笑]`
        """
        ok, note = await self.handle_tts(event, str(text))
        if not ok:
            yield event.plain_result(f"❌ {note}")

    @filter.command("mimo引用", alias={"mimo读引用", "mimo_quote"})
    async def cmd_mimo_quote(self, event: AstrMessageEvent):
        """朗读被引用的消息：/mimo引用（需先引用一条文本消息）"""
        quoted = self.extract_quoted_text(event)
        if not quoted:
            yield event.plain_result("请先引用一条含文本的消息，再发送 /mimo引用")
            return
        ok, note = await self.handle_tts(event, quoted)
        if not ok:
            yield event.plain_result(f"❌ {note}")

    @filter.command("mimo音色", alias={"mimo_voice"})
    async def cmd_mimo_voice(self, event: AstrMessageEvent, name: str = ""):
        """查看或切换音色：/mimo音色 [音色名]"""
        session_id = event.get_session_id()
        candidate = (
            self.parse_command_args(event, ("mimo音色", "mimo_voice"))
            or (name or "").strip()
        )

        if not candidate:
            current = self.state.get(session_id, "voice", self.default_voice)
            yield event.plain_result(
                f"当前音色：{current}\n可选：{'、'.join(VOICE_CHOICES)}\n"
                f"用法：/mimo音色 冰糖（发送“重置”恢复默认）"
            )
            return

        if candidate in ("重置", "reset", "默认", "default"):
            await self.state.set(session_id, "voice", "")
            yield event.plain_result(f"已恢复默认音色：{self.default_voice}")
            return

        matched = next((v for v in VOICE_CHOICES if v.lower() == candidate.lower()), "")
        if not matched:
            yield event.plain_result(
                f"未知音色：{candidate}\n可选：{'、'.join(VOICE_CHOICES)}"
            )
            return

        await self.state.set(session_id, "voice", matched)
        yield event.plain_result(f"✅ 本会话音色已切换为：{matched}")

    @filter.command("mimo风格", alias={"mimo_style"})
    async def cmd_mimo_style(self, event: AstrMessageEvent, style: str = ""):
        """查看或切换默认风格：/mimo风格 [风格词]"""
        session_id = event.get_session_id()
        candidate = self.parse_command_args(event, ("mimo风格", "mimo_style")) or (
            str(style).strip() if style else ""
        )

        if not candidate:
            current = (
                self.state.get(session_id, "style") or self.style_prefix or "（未设置）"
            )
            yield event.plain_result(
                f"当前默认风格：{current}\n"
                "示例：/mimo风格 温柔 撒娇 → 合成时会自动加 `(温柔 撒娇)` 前缀\n"
                "发送“重置”可恢复插件默认。"
            )
            return

        if candidate in ("重置", "reset", "清空", "clear", "默认", "default"):
            await self.state.set(session_id, "style", "")
            yield event.plain_result("已清除本会话风格设置")
            return

        await self.state.set(session_id, "style", candidate)
        yield event.plain_result(f"✅ 本会话默认风格已设为：{candidate}")

    @filter.command("mimo状态", alias={"mimo_status"})
    async def cmd_mimo_status(self, event: AstrMessageEvent):
        """查看插件运行状态：/mimo状态"""
        session_id = event.get_session_id()
        key = self.resolve_api_key()
        native = self.native_tts_info(force=True)
        cache_files = (
            len(list(self.cache_dir.glob("mimo_*"))) if self.cache_dir.is_dir() else 0
        )
        platform = (event.get_platform_name() or "").lower()
        native_state = "启用" if native.get("tts_enabled") == "1" else "未启用"
        native_provider = native.get("provider_id") or native.get("tts_provider_id") or "-"
        lines = [
            "【MiMo TTS 状态】",
            f"· 启用：{'是' if self.enable else '否'}",
            f"· 自动发声：{'开启' if self.enable_auto_hook else '关闭'}"
            f"（概率 {self.auto_speak_probability:.2f}，模式 {self.send_mode}，"
            f"字数 {self.min_text_len}~{self.max_text_len}）",
            f"· 提示词注入：{'开启' if self.enable_prompt_injection else '关闭'}",
            f"· 模型：{self.model}",
            f"· 默认音色：{self.default_voice}（本会话：{self.state.get(session_id, 'voice', self.default_voice)}）",
            f"· 默认风格：{self.state.get(session_id, 'style') or self.style_prefix or self.default_style or '未设置'}",
            f"· 动作标签：{'开启' if self.enable_audio_tags else '关闭'}",
            f"· API Key：{'已配置' if key else '缺失'}（来源：{self.resolve_api_key_source()}）",
            f"· 请求端点：{self.resolve_api_url()}",
            f"· 原生 TTS：{native_state}（provider={native_provider}）",
            f"· 原生配置：{native.get('config_path') or '未找到 cmd_config.json'}",
            f"· 当前平台：{platform or '未知'}（启用列表：{', '.join(self.target_platforms) or '全部'}）",
            f"· ffmpeg：{'可用' if self._ffmpeg_path else '缺失'}",
            f"· 微信语音策略：{self.weixin_voice_mode}；QQ 录音格式：{self.qq_record_format}",
            f"· 临时音频：{cache_files} 个（TTL {self.temp_ttl_seconds}s）",
        ]
        if native.get("tts_enabled") == "1" and self.send_mode == SEND_MODE_WITH_TEXT:
            lines.append(
                "· ⚠️ 原生 TTS 与「文字+语音」并存会导致重合发声，"
                "请按 README 关闭原生 TTS，或改用 voice_only。"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("mimo帮助", alias={"mimo_help"})
    async def cmd_mimo_help(self, event: AstrMessageEvent):
        """显示 MiMo TTS 使用帮助"""
        yield event.plain_result(
            "【MiMo TTS 帮助】\n"
            "· /mimo <文本>          合成语音条\n"
            "· /mimo引用             朗读被引用的消息\n"
            "· /mimo音色 [名称]      查看/切换音色\n"
            "· /mimo风格 [风格词]    查看/设置默认风格\n"
            "· /mimo状态             查看运行状态\n"
            "\n自动语音（无需指令）：\n"
            f"· 自动发声：{'开启' if self.enable_auto_hook else '关闭'}"
            f"（概率 {self.auto_speak_probability:.2f}，模式 {self.send_mode}）\n"
            f"· 字数闸门：{self.min_text_len}~{self.max_text_len} 字，"
            "代码块/链接/加粗等 Markdown 会被自动清理\n"
            "\n文本语法（官方协议）：\n"
            "· 风格：`(温柔 撒娇)你好呀`，或写 `<style>温柔 撒娇</style>你好呀`（自动转换）\n"
            "· 动作标签：`你好呀[轻笑]`、`唉[叹气]`\n"
            f"· 内置音色：{'、'.join(VOICE_CHOICES)}"
        )

    # ---------------- LLM 工具 ----------------

    @filter.llm_tool(name="mimo_speak")
    async def llm_mimo_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        style: str = "",
        voice: str = "",
    ) -> str:
        """当用户希望你「用语音说 / 发语音条 / 念出来」时，把文本合成为语音条发送。

        Args:
            text(string): 要朗读的文本，可含 `[轻笑]` 等动作标签
            style(string): 风格词，如「温柔 撒娇」「慵懒」，可为空
            voice(string): 音色名，可选 冰糖/茉莉/苏打/白桦/Mia/Chloe/Milo/Dean，可为空
        """
        if not self.enable_llm_tool:
            return "语音工具已关闭（enable_llm_tool=false）"
        ok, note = await self.handle_tts(event, text, voice=voice, style=style)
        if ok:
            # 标记本轮已发声，避免 on_decorating_result 钩子对最终文本再发一次
            event.set_extra(HOOK_SPOKEN_EXTRA_KEY, True)
            return f"语音已发送（{note}）"
        return f"语音发送失败：{note}"
