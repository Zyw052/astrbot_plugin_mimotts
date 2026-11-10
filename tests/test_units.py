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
    return {
        k: v.get("default")
        for k, v in json.loads(
            (PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8")
        ).items()
    }


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

    def get_platform_name(self) -> str:
        return self._platform

    def get_session_id(self) -> str:
        return self._session_id

    def get_sender_id(self) -> str:
        return "10001"

    async def send(self, chain) -> None:
        self.sent.append(chain)


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
