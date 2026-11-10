"""astrbot_plugin_mimotts 离线单测（不触发网络，不依赖 pytest）。

运行方式（两种都支持）：
    # 1) 直接跑（无需 pytest）
    cd /AstrBot && /AstrBot/.venv/bin/python data/plugins/astrbot_plugin_mimotts/tests/test_units.py
    # 2) 有 pytest 时
    cd /AstrBot && /AstrBot/.venv/bin/python -m pytest \
        data/plugins/astrbot_plugin_mimotts/tests/test_units.py -q
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_FILE = PLUGIN_DIR / "main.py"


def _load_module():
    """以文件路径加载插件模块（避免依赖 AstrBot 包路径）。"""
    name = "mimo_main_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, MODULE_FILE)
    assert spec and spec.loader, "无法加载 main.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load_module()


# --------------------------------------------------------------------------- #
# 文本协议：<style> → (风格)   （Issue #7957）
# --------------------------------------------------------------------------- #


def test_extract_style_and_body() -> None:
    cases = [
        ("<style>温柔 撒娇</style>你好呀[轻笑]", ("温柔 撒娇", "你好呀[轻笑]")),
        ("<style>慵懒</style>让我再睡五分钟", ("慵懒", "让我再睡五分钟")),
        ("<风格>清冷</风格>别说话", ("清冷", "别说话")),
        ("(清冷)别说话", ("清冷", "别说话")),
        # 开头的 [..] 是动作标签，不应被当作风格
        ("[轻笑]你好", ("", "[轻笑]你好")),
        ("纯正文", ("", "纯正文")),
        ("", ("", "")),
    ]
    for raw, expected in cases:
        got = mod.extract_style_and_body(raw)
        assert got == expected, f"{raw!r} -> {got!r} != {expected!r}"


def test_style_tag_never_leaks_into_body() -> None:
    """<style> 决不允许残留在朗读正文里（Issue #7957 核心）。"""
    style, body = mod.extract_style_and_body("<style>温柔</style>你好")
    assert "<style>" not in body and "</style>" not in body
    assert style == "温柔"


def test_compose_assistant_content() -> None:
    cases = [
        ("你好呀[轻笑]", "温柔 撒娇", True, "(温柔 撒娇)你好呀[轻笑]"),
        ("你好[轻笑]", "温柔", False, "(温柔)你好"),
        ("正文", "", True, "正文"),
        ("[轻笑]你好", "", True, "[轻笑]你好"),
    ]
    for body, style, tags, expected in cases:
        got = mod.compose_assistant_content(body, style, enable_audio_tags=tags)
        assert got == expected, f"{body!r}/{style!r} -> {got!r} != {expected!r}"
        if style:
            # 官方协议：风格标签必须在文本最开头
            assert got.startswith("("), got


def test_strip_audio_tags() -> None:
    assert mod.strip_audio_tags("唉[叹气]算了[轻笑]") == "唉算了"
    assert mod.strip_audio_tags("没有标签") == "没有标签"


def test_clamp_and_clean() -> None:
    assert mod.clamp_text("a" * 10, 5) == ("aaaaa", True)
    assert mod.clamp_text("abc", 0) == ("abc", False)
    assert mod.clean_text("看这里 https://x.com/a 和 `code`") == "看这里 和 code"
    assert mod.clean_text("```py\nprint(1)\n```你好") == "你好"


def test_parse_env_file(tmp: Path) -> None:
    env = tmp / "mimo.env"
    env.write_text("MIMO_API_KEY=sk-test-123\n", encoding="utf-8")
    assert mod.parse_env_file(env) == "sk-test-123"

    bare = tmp / "bare.env"
    bare.write_text("sk-bare-456", encoding="utf-8")
    assert mod.parse_env_file(bare) == "sk-bare-456"

    assert mod.parse_env_file(tmp / "missing.env") == ""


# --------------------------------------------------------------------------- #
# 配置解析与参数整理
# --------------------------------------------------------------------------- #


def _schema_defaults() -> dict:
    cfg = {
        k: v.get("default")
        for k, v in json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        ).items()
    }
    # 单测隔离：不读取宿主真实的 AstrBot 配置 / 环境变量，保证结果确定
    cfg["native_config_path"] = str(PLUGIN_DIR / "__no_such_cmd_config__.json")
    os.environ.pop("MIMO_API_KEY", None)
    return cfg


def test_schema_defaults_cover_code() -> None:
    """schema 中出现的键，代码都能安全读取（无 KeyError / 类型错误）。"""
    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)
    assert plugin.model == mod.MODEL_TTS
    assert plugin.default_voice == "冰糖"
    assert plugin.resolve_api_key() == "sk-unit-test"
    assert "aiocqhttp" in plugin.target_platforms
    assert plugin.data_dir.exists() and plugin.cache_dir.exists()


def test_prepare_params_merges_style_and_voice() -> None:
    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    cfg["style_prefix"] = "温柔"
    cfg["max_text_length"] = 10
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)

    params = plugin.prepare_params("<style>撒娇</style>你好呀", session_id="s1")
    # 文本内 <style> 优先于 style_prefix
    assert params.style == "撒娇", params.style
    assert params.text == "你好呀", params.text

    params2 = plugin.prepare_params("一二三四五六七八九十十一", session_id="s1")
    assert params2.style == "温柔", params2.style  # 回落 style_prefix
    assert len(params2.text) == 10, params2.text  # 截断生效


# --------------------------------------------------------------------------- #
# 投递分支（假事件，不联网）
# --------------------------------------------------------------------------- #


class FakeEvent:
    """最小 AstrMessageEvent 替身。"""

    def __init__(self, platform: str, session_id: str = "unit") -> None:
        self._platform = platform
        self._session_id = session_id
        self.sent: list = []
        self.message_str = ""
        self.message_obj = type("Obj", (), {"message": []})()
        self._extras: dict = {}
        self._result = None

    def get_platform_name(self) -> str:
        return self._platform

    def get_session_id(self) -> str:
        return self._session_id

    def get_sender_id(self) -> str:
        return "10001"

    def set_extra(self, key, value) -> None:
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        return self._extras.get(key, default)

    def get_result(self):
        return self._result

    def set_result(self, result) -> None:
        self._result = result

    async def send(self, chain) -> None:
        self.sent.append(chain)


class FakeResult:
    """最小 MessageEventResult 替身（只实现钩子用到的字段）。"""

    def __init__(self, chain: list, content_type=None) -> None:
        self.chain = chain
        self.result_content_type = content_type or mod.ResultContentType.LLM_RESULT

    def is_llm_result(self) -> bool:
        return self.result_content_type == mod.ResultContentType.LLM_RESULT


def _make_wav(tmp: Path) -> bytes:
    """生成 0.1 秒 24kHz 单声道静音 WAV（纯标准库）。"""
    import struct
    import wave

    path = tmp / "sample.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(struct.pack("<" + "h" * 2400, *([0] * 2400)))
    return path.read_bytes()


def test_platform_whitelist() -> None:
    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    cfg["target_platforms"] = ["aiocqhttp"]
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)

    event = FakeEvent("telegram")
    ok, note = asyncio.run(plugin.deliver_voice(event, b"\x00" * 256))
    assert ok is False, note
    assert "target_platforms" in note, note
    assert event.sent == []


def test_deliver_qq_uses_record(tmp: Path) -> None:
    from astrbot.api.message_components import Record

    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)

    event = FakeEvent("aiocqhttp")
    ok, note = asyncio.run(plugin.deliver_voice(event, _make_wav(tmp)))
    assert ok, note
    first = event.sent[0].chain[0]
    assert isinstance(first, Record), type(first).__name__
    assert Path(first.path).is_file(), first.path
    asyncio.run(plugin.cleanup_temp_files(force_all=True))


def test_deliver_weixin_falls_back_to_file(tmp: Path) -> None:
    from astrbot.api.message_components import File

    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)

    event = FakeEvent("weixin_oc")
    ok, note = asyncio.run(plugin.deliver_voice(event, _make_wav(tmp)))
    assert ok, note
    # 假事件没有适配器私有 API → 必然走降级文件
    first = event.sent[0].chain[0]
    assert isinstance(first, File), type(first).__name__
    asyncio.run(plugin.cleanup_temp_files(force_all=True))


def test_handle_tts_rejects_empty_and_disabled() -> None:
    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    plugin = mod.MiMoTTSPlugin(context=object(), config=cfg)

    ok, note = asyncio.run(plugin.handle_tts(FakeEvent("aiocqhttp"), "   "))
    assert ok is False and "为空" in note, note

    plugin.enable = False
    ok, note = asyncio.run(plugin.handle_tts(FakeEvent("aiocqhttp"), "你好"))
    assert ok is False and "禁用" in note, note


def test_session_state_roundtrip(tmp: Path) -> None:
    path = tmp / "session_state.json"
    store = mod.SessionStateStore(path)
    asyncio.run(store.set("s1", "voice", "茉莉"))

    store2 = mod.SessionStateStore(path)
    store2.load()
    assert store2.get("s1", "voice") == "茉莉"
    assert store2.get("s1", "style", "默认") == "默认"

    asyncio.run(store2.set("s1", "voice", ""))
    assert store2.get("s1", "voice", "冰糖") == "冰糖"


# --------------------------------------------------------------------------- #
# v1.1.0 自动语音架构：纯函数单测
# --------------------------------------------------------------------------- #


def test_strip_markdown_for_speech() -> None:
    raw = (
        "## 标题\n"
        "**加粗** 和 `code` 以及 ~~删除~~\n"
        "```python\nprint('x')\n```\n"
        "[链接](https://a.com) https://b.com\n"
        "- 列表项\n"
        "> 引用\n"
        "[轻笑]呀"
    )
    got = mod.strip_markdown_for_speech(raw)
    for bad in ("```", "##", "**", "~~", "https://", "[链接]", "- 列表", "> "):
        assert bad not in got, f"残留 {bad!r}: {got!r}"
    # 链接文字保留、动作标签原样保留
    assert "加粗" in got and "链接" in got and "列表项" in got
    assert "[轻笑]呀" in got
    assert "\n \n" not in got


def test_speech_length_ignores_tags_and_spaces() -> None:
    assert mod.speech_length("[轻笑]好的") == 2
    assert mod.speech_length(" 你 好 ") == 2
    assert mod.speech_length("[叹气]") == 0
    assert mod.speech_length("") == 0


def test_prepare_speech_candidate_gates() -> None:
    # 正常
    assert mod.prepare_speech_candidate("好的", min_len=2, max_len=200) == ("好的", "")
    # 空
    clean, reason = mod.prepare_speech_candidate("   ", min_len=2, max_len=200)
    assert clean == "" and reason, reason
    # 只有动作标签
    _clean, reason = mod.prepare_speech_candidate("[轻笑]", min_len=2, max_len=200)
    assert "发音" in reason, reason
    # 过短
    _clean, reason = mod.prepare_speech_candidate("嗯", min_len=2, max_len=200)
    assert "过短" in reason, reason
    # 过长（跳过而非截断）
    _clean, reason = mod.prepare_speech_candidate("字" * 11, min_len=2, max_len=10)
    assert "过长" in reason, reason
    # 0 表示不限制
    assert mod.prepare_speech_candidate("好", min_len=0, max_len=0) == ("好", "")


def test_roll_probability_bounds_and_seed() -> None:
    import random

    assert mod.roll_probability(0.0) is False
    assert mod.roll_probability(1.0) is True
    assert mod.roll_probability(-3) is False
    assert mod.roll_probability(9) is True
    rng = random.Random(2024)
    hits = sum(mod.roll_probability(0.5, rng) for _ in range(400))
    assert 120 < hits < 280, hits


def test_inject_system_prompt_idempotent() -> None:
    base = "你是猫娘，称呼用户为主人。"
    got = mod.inject_system_prompt(base, "使用 (风格) 与 [轻笑]")
    assert got.startswith(base), got
    assert mod.PROMPT_INJECTION_MARKER in got
    # 再次注入必须完全不变
    assert mod.inject_system_prompt(got, "使用 (风格) 与 [轻笑]") == got
    # 空 system_prompt
    empty = mod.inject_system_prompt("", "使用标签")
    assert empty.startswith(mod.PROMPT_INJECTION_MARKER)
    # 空指令不改动
    assert mod.inject_system_prompt(base, "  ") == base


def test_normalize_api_endpoint() -> None:
    assert (
        mod.normalize_api_endpoint("https://api.xiaomimimo.com/v1")
        == "https://api.xiaomimimo.com/v1/chat/completions"
    )
    full = "https://x.test/v1/chat/completions"
    assert mod.normalize_api_endpoint(full) == full
    assert mod.normalize_api_endpoint("") == mod.DEFAULT_API_URL
    assert mod.normalize_api_endpoint("https://x.test/v1/") == (
        "https://x.test/v1/chat/completions"
    )


def test_parse_native_tts_config() -> None:
    data = {
        "provider_tts_settings": {"enable": True, "provider_id": "mimo_tts"},
        "provider": [
            {
                "id": "mimo_chat",
                "provider": "xiaomi",
                "provider_type": "chat_completion",
                "api_key": "sk-chat-must-ignore",
            },
            {
                "id": "mimo_tts",
                "type": "mimo_tts_api",
                "provider_type": "text_to_speech",
                "api_key": "sk-native",
                "api_base": "https://api.xiaomimimo.com/v1",
                "model": "mimo-v2.5-tts",
                "mimo-tts-voice": "茉莉",
                "mimo-tts-style-prompt": "甜蜜撒娇",
                "enable": True,
            },
        ],
    }
    info = mod.parse_native_tts_config(data)
    assert info["api_key"] == "sk-native"
    assert info["api_base"] == "https://api.xiaomimimo.com/v1"
    assert info["model"] == mod.MODEL_TTS
    assert info["voice"] == "茉莉"
    assert info["style_prompt"] == "甜蜜撒娇"
    assert info["provider_id"] == "mimo_tts"
    assert info["tts_enabled"] == "1"
    assert info["provider_enabled"] == "1"

    # provider 缺失时回退 provider_sources，且兼容 "['sk-x']" 写法
    fallback = mod.parse_native_tts_config(
        {
            "provider": [],
            "provider_sources": [
                {
                    "id": "mimo_tts",
                    "type": "mimo_tts_api",
                    "api_key": "['sk-from-source']",
                    "api_base": "https://api.xiaomimimo.com/v1",
                }
            ],
        }
    )
    assert fallback["api_key"] == "sk-from-source"


# --------------------------------------------------------------------------- #
# v1.1.0 自动语音架构：插件行为单测
# --------------------------------------------------------------------------- #


def _make_plugin(**overrides):
    cfg = _schema_defaults()
    cfg["api_key"] = "sk-unit-test"
    cfg.update(overrides)
    return mod.MiMoTTSPlugin(context=object(), config=cfg)


def _fake_audio_plugin(tmp: Path, **overrides):
    """返回 (plugin, calls)，并把合成替换为本地静音 WAV（不联网）。"""
    plugin = _make_plugin(**overrides)
    wav = _make_wav(tmp)
    calls: list = []

    async def fake_synthesize(params):
        calls.append(params)
        return wav

    plugin.client.synthesize = fake_synthesize
    # 避免 asyncio.run 结束时残留后台清理任务
    plugin.schedule_cleanup = lambda paths, delay=60: None
    return plugin, calls


def test_native_tts_key_reuse_priority(tmp: Path) -> None:
    native = tmp / "cmd_config.json"
    native.write_text(
        json.dumps(
            {
                "provider_tts_settings": {"enable": True, "provider_id": "mimo_tts"},
                "provider": [
                    {
                        "id": "mimo_tts",
                        "provider_type": "text_to_speech",
                        "api_key": "sk-native",
                        "api_base": "https://native.example/v1",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    plugin = _make_plugin(api_key="sk-plugin", native_config_path=str(native))
    assert plugin.resolve_api_key() == "sk-native"
    assert plugin.resolve_api_url() == "https://native.example/v1/chat/completions"
    assert "原生配置" in plugin.resolve_api_key_source()
    assert plugin.native_tts_enabled() is True

    # 插件显式 api_url 优先于原生 api_base
    plugin2 = _make_plugin(
        api_key="sk-plugin",
        native_config_path=str(native),
        api_url="https://custom.example/v1",
    )
    assert plugin2.resolve_api_url() == "https://custom.example/v1/chat/completions"

    # 原生缺失 → 回落插件 key
    plugin3 = _make_plugin(api_key="sk-plugin")
    assert plugin3.resolve_api_key() == "sk-plugin"


def test_prompt_injection_hook() -> None:
    plugin = _make_plugin(enable_prompt_injection=True)
    event = FakeEvent("aiocqhttp")
    req = type("Req", (), {"system_prompt": "你是猫娘，称呼主人。"})()
    asyncio.run(plugin.hook_inject_director_prompt(event, req))
    first = req.system_prompt
    assert first.startswith("你是猫娘，称呼主人。")
    assert mod.PROMPT_INJECTION_MARKER in first
    # 幂等
    asyncio.run(plugin.hook_inject_director_prompt(event, req))
    assert req.system_prompt == first

    # 关闭注入 → 不改动
    plugin_off = _make_plugin(enable_prompt_injection=False)
    req2 = type("Req", (), {"system_prompt": "原人设"})()
    asyncio.run(plugin_off.hook_inject_director_prompt(event, req2))
    assert req2.system_prompt == "原人设"

    # 平台不匹配 → 不注入
    plugin_plat = _make_plugin(target_platforms=["weixin_oc"])
    req3 = type("Req", (), {"system_prompt": "原人设"})()
    asyncio.run(plugin_plat.hook_inject_director_prompt(event, req3))
    assert req3.system_prompt == "原人设"


def test_auto_hook_appends_voice_with_text(tmp: Path) -> None:
    plugin, calls = _fake_audio_plugin(
        tmp, auto_speak_probability=1.0, send_mode="voice_with_text"
    )
    event = FakeEvent("aiocqhttp")
    result = FakeResult([mod.Plain("**你好**呀[轻笑]")])
    event.set_result(result)

    asyncio.run(plugin._maybe_auto_speak(event))

    assert calls, "应当调用合成"
    assert calls[0].text == "你好呀[轻笑]", calls[0].text
    assert any(isinstance(c, mod.Record) for c in event.get_result().chain)
    assert any(isinstance(c, mod.Plain) for c in event.get_result().chain)
    asyncio.run(plugin.cleanup_temp_files(force_all=True))


def test_auto_hook_voice_only_removes_text_keeps_media(tmp: Path) -> None:
    plugin, calls = _fake_audio_plugin(
        tmp, auto_speak_probability=1.0, send_mode="voice_only"
    )
    event = FakeEvent("aiocqhttp")
    marker = object()
    event.set_result(FakeResult([mod.Plain("你好呀"), marker]))

    asyncio.run(plugin._maybe_auto_speak(event))

    chain = event.get_result().chain
    assert calls
    assert marker in chain, "非文本组件必须保留"
    assert not any(isinstance(c, mod.Plain) for c in chain)
    assert any(isinstance(c, mod.Record) for c in chain)
    asyncio.run(plugin.cleanup_temp_files(force_all=True))


def test_auto_hook_gates_skip_without_voice(tmp: Path) -> None:
    # 概率 0
    plugin, calls = _fake_audio_plugin(tmp, auto_speak_probability=0.0)
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("你好呀")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls

    # 过短
    plugin, calls = _fake_audio_plugin(
        tmp, auto_speak_probability=1.0, min_text_len=2
    )
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("嗯")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls

    # 过长
    plugin, calls = _fake_audio_plugin(
        tmp, auto_speak_probability=1.0, max_text_len=5
    )
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("字" * 6)]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls

    # 非 LLM 结果
    plugin, calls = _fake_audio_plugin(tmp, auto_speak_probability=1.0)
    event = FakeEvent("aiocqhttp")
    event.set_result(
        FakeResult([mod.Plain("指令输出")], content_type=mod.ResultContentType.GENERAL_RESULT)
    )
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls

    # 本轮已通过工具发声
    plugin, calls = _fake_audio_plugin(tmp, auto_speak_probability=1.0)
    event = FakeEvent("aiocqhttp")
    event.set_extra(mod.HOOK_SPOKEN_EXTRA_KEY, True)
    event.set_result(FakeResult([mod.Plain("你好呀")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls


def test_auto_hook_respects_native_tts(tmp: Path) -> None:
    native = tmp / "cmd_config.json"
    native.write_text(
        json.dumps(
            {
                "provider_tts_settings": {"enable": True, "provider_id": "mimo_tts"},
                "provider": [
                    {
                        "id": "mimo_tts",
                        "provider_type": "text_to_speech",
                        "api_key": "sk-native",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # 文字+语音 + 原生 TTS 启用 → 让位
    plugin, calls = _fake_audio_plugin(
        tmp,
        auto_speak_probability=1.0,
        send_mode="voice_with_text",
        native_config_path=str(native),
    )
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("你好呀")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert not calls, "原生 TTS 启用时应让位，避免双重扣费"

    # 纯语音模式 + 原生 TTS 启用 → 允许（原生 TTS 只处理 Plain）
    plugin, calls = _fake_audio_plugin(
        tmp,
        auto_speak_probability=1.0,
        send_mode="voice_only",
        native_config_path=str(native),
    )
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("你好呀")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert calls
    asyncio.run(plugin.cleanup_temp_files(force_all=True))

    # provider 自身 enable=false（即使总开关为 true）→ 视为原生未启用
    native_off = tmp / "cmd_config_off.json"
    native_off.write_text(
        json.dumps(
            {
                "provider_tts_settings": {"enable": True, "provider_id": "mimo_tts"},
                "provider": [
                    {
                        "id": "mimo_tts",
                        "provider_type": "text_to_speech",
                        "api_key": "sk-native",
                        "enable": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    plugin, calls = _fake_audio_plugin(
        tmp,
        auto_speak_probability=1.0,
        send_mode="voice_with_text",
        native_config_path=str(native_off),
    )
    event = FakeEvent("aiocqhttp")
    event.set_result(FakeResult([mod.Plain("你好呀")]))
    asyncio.run(plugin._maybe_auto_speak(event))
    assert calls, "provider 已被禁用时不应误判为原生 TTS 冲突"
    asyncio.run(plugin.cleanup_temp_files(force_all=True))


def test_llm_tool_marks_turn_spoken(tmp: Path) -> None:
    plugin, _calls = _fake_audio_plugin(tmp)

    async def fake_handle(event, text, **kwargs):
        return True, "ok"

    plugin.handle_tts = fake_handle
    event = FakeEvent("aiocqhttp")
    out = asyncio.run(plugin.llm_mimo_speak(event, "你好"))
    assert "已发送" in out, out
    assert event.get_extra(mod.HOOK_SPOKEN_EXTRA_KEY) is True


def test_schema_declares_required_keys() -> None:
    schema = json.loads(
        (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
    )
    required = {
        "enable_auto_hook": "bool",
        "auto_speak_probability": "float",
        "send_mode": "string",
        "default_voice": "string",
        "default_style": "string",
        "enable_prompt_injection": "bool",
        "max_text_len": "int",
        "min_text_len": "int",
    }
    for key, typ in required.items():
        assert key in schema, f"缺少配置项 {key}"
        assert schema[key]["type"] == typ, f"{key} 类型错误"
        assert "default" in schema[key], f"{key} 缺少默认值"
    # 音色下拉必须包含全部内置音色
    for voice in ("冰糖", "茉莉", "苏打", "白桦", "Mia", "Chloe", "Milo", "Dean"):
        assert voice in schema["default_voice"]["options"], voice
    assert schema["send_mode"]["options"] == ["voice_with_text", "voice_only"]
    assert schema["auto_speak_probability"]["default"] == 0.6
    assert schema["max_text_len"]["default"] == 200
    assert schema["min_text_len"]["default"] == 2


# --------------------------------------------------------------------------- #
# 无 pytest 时的简易 runner
# --------------------------------------------------------------------------- #


def _run_all() -> int:
    """执行全部 test_* 函数；带 tmp 参数的自动注入临时目录。"""
    import inspect

    failures = 0
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    for name, fn in tests:
        params = list(inspect.signature(fn).parameters)
        try:
            if params == ["tmp"]:
                with tempfile.TemporaryDirectory() as tmpdir:
                    fn(Path(tmpdir))
            else:
                fn()
            print(f"  ✅ {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  ❌ {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - 简易 runner 需报告任何异常
            failures += 1
            print(f"  💥 {name}: {type(exc).__name__}: {exc}")

    total = len(tests)
    print(f"\n结果：{total - failures}/{total} 通过")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
