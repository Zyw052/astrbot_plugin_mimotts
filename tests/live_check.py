"""真实 API 联调自检（会消耗 MiMo 额度，不放进 pytest 自动执行）。

用法：
    cd /AstrBot && /AstrBot/.venv/bin/python \
        data/plugins/astrbot_plugin_mimotts/tests/live_check.py

校验点：StarManager 同款导入 → 实例化 → handler 注册 → 真实合成 →
NapCat(Record/SILK) / 微信(降级 File) / 白名单 / caption / 会话态 / 清理。
"""

import asyncio
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, "/AstrBot")

PLUGIN_DIR = Path("/data/apps/astrbot/data/plugins/astrbot_plugin_mimotts")
MODULE_PATH = "data.plugins.astrbot_plugin_mimotts.main"

# ---- 1. 与 StarManager 一致的导入方式（__import__(path, fromlist=[module_str])）
mod = __import__(MODULE_PATH, fromlist=["main"])
print(f"✅ 模块导入成功: {mod.__name__}")

from astrbot.core.message.components import File, Plain, Record
from astrbot.core.star.base import Star

star_classes = [
    obj
    for name, obj in vars(mod).items()
    if inspect.isclass(obj)
    and issubclass(obj, Star)
    and obj is not Star
    and obj.__module__ == mod.__name__
]
assert len(star_classes) == 1, f"应恰好 1 个 Star 子类，实际 {star_classes}"
cls = star_classes[0]
print(f"✅ Star 子类: {cls.__name__}")

# ---- 2. 配置：取 _conf_schema.json 的默认值
schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
config = {k: v.get("default") for k, v in schema.items()}
config["api_key"] = ""  # 走凭证文件回落路径
config["debug"] = True


class DummyContext:
    """最小 Context 替身（插件只用到 logger/发送，不依赖注册期能力）。"""


async def build():
    return cls(context=DummyContext(), config=config)


plugin = asyncio.run(build())
print(f"✅ 实例化成功: {plugin.__class__.__name__} data_dir={plugin.data_dir}")
assert plugin.data_dir.exists() and plugin.cache_dir.exists()
assert plugin.resolve_api_key().startswith("sk-"), "API Key 解析失败"
print(f"✅ API Key 解析（凭证文件回落）: {plugin.resolve_api_key()[:6]}…")

# ---- 3. Handler 注册检查
from astrbot.core.star.star_handler import star_handlers_registry

handlers = [
    h
    for h in star_handlers_registry
    if getattr(h, "handler_module_path", "").startswith(MODULE_PATH)
    or getattr(h, "handler", None) is not None
    and getattr(h.handler, "__module__", "") == mod.__name__
]
names = sorted({getattr(h, "handler_name", "?") for h in handlers})
print(f"✅ 已注册 handler {len(handlers)} 个: {names}")
assert any("mimo" in n for n in names), names


# ---- 4. 假事件，跑通各平台分支
class FakeEvent:
    def __init__(self, platform: str, session_id: str = "test_session"):
        self._platform = platform
        self._session_id = session_id
        self.sent = []
        self.message_str = ""
        self.message_obj = type("Obj", (), {"message": []})()

    def get_platform_name(self):
        return self._platform

    def get_session_id(self):
        return self._session_id

    def get_sender_id(self):
        return "10001"

    async def send(self, chain):
        self.sent.append(chain)


async def main():
    await plugin.initialize()

    # 真实合成一次
    params = plugin.prepare_params(
        "<style>温柔 撒娇</style>你好呀，测试语音条[轻笑]", session_id="test_session"
    )
    assert params.style == "温柔 撒娇", params.style
    assert params.text == "你好呀，测试语音条[轻笑]", params.text
    wav = await plugin.client.synthesize(params)
    print(f"✅ 真实合成 {len(wav)} bytes；assistant 文本前缀校验通过")

    # 4.1 NapCat / OneBot v11 → Record
    ev_qq = FakeEvent("aiocqhttp")
    ok, note = await plugin.deliver_voice(ev_qq, wav, caption="测试")
    assert ok, note
    assert len(ev_qq.sent) == 1
    comps = ev_qq.sent[0].chain
    assert isinstance(comps[0], Record), [type(c).__name__ for c in comps]
    qq_path = Path(comps[0].path)
    assert qq_path.is_file(), qq_path
    print(
        f"✅ NapCat 分支：Record 语音段 -> {qq_path.name} ({qq_path.stat().st_size} bytes) [{note}]"
    )

    # 4.2 微信 auto（无适配器私有 API）→ 降级 File
    ev_wx = FakeEvent("weixin_oc")
    ok, note = await plugin.deliver_voice(ev_wx, wav)
    assert ok, note
    comps = ev_wx.sent[0].chain
    assert isinstance(comps[0], File), [type(c).__name__ for c in comps]
    wx_path = Path(comps[0].file_)
    assert wx_path.is_file() and wx_path.suffix == ".mp3", wx_path
    print(
        f"✅ 微信分支：降级 File -> {wx_path.name} ({wx_path.stat().st_size} bytes) [{note}]"
    )

    # 4.3 微信 file 模式
    plugin.weixin_voice_mode = "file"
    ev_wx2 = FakeEvent("weixin_oc")
    ok, note = await plugin.deliver_voice(ev_wx2, wav)
    assert ok and isinstance(ev_wx2.sent[0].chain[0], File), note
    print(f"✅ 微信 file 模式：File -> {ev_wx2.sent[0].chain[0].file_}")

    # 4.4 平台未启用 → 明确报错
    plugin.target_platforms = ["aiocqhttp"]
    ev_tg = FakeEvent("telegram")
    ok, note = await plugin.deliver_voice(ev_tg, wav)
    assert not ok and "未在 target_platforms" in note, note
    print(f"✅ 平台白名单生效：{note}")
    plugin.target_platforms = ["aiocqhttp", "weixin_oc", "qq_official"]

    # 4.5 语音条 + 文字 caption
    plugin.send_text_caption = True
    ev_qq2 = FakeEvent("aiocqhttp")
    ok, _ = await plugin.deliver_voice(ev_qq2, wav, caption="文字说明")
    assert ok and isinstance(ev_qq2.sent[0].chain[0], Plain)
    assert isinstance(ev_qq2.sent[1].chain[0], Record)
    print("✅ caption 分支：先文字后语音")

    # 4.6 会话级音色/风格持久化
    await plugin.state.set("test_session", "voice", "茉莉")
    await plugin.state.set("test_session", "style", "慵懒")
    p2 = plugin.prepare_params("再来一句", session_id="test_session")
    assert p2.voice == "茉莉" and p2.style == "慵懒", (p2.voice, p2.style)
    print("✅ 会话级音色/风格生效并落盘")

    # 4.7 空文本 / 禁用 分支
    ok, note = await plugin.handle_tts(FakeEvent("aiocqhttp"), "   ")
    assert not ok and "为空" in note, note
    plugin.enable = False
    ok, note = await plugin.handle_tts(FakeEvent("aiocqhttp"), "你好")
    assert not ok and "禁用" in note, note
    plugin.enable = True
    print("✅ 空文本 / 禁用 分支正常拒绝")

    # 4.8 临时文件清理
    removed = await plugin.cleanup_temp_files(force_all=True)
    left = list(plugin.cache_dir.glob("mimo_*"))
    print(f"✅ 清理临时文件 {removed} 个，剩余 {len(left)} 个")
    assert not left, left

    await plugin.client.close()
    await plugin.terminate()


asyncio.run(main())
print("\n🎉 全部加载与投递分支测试通过")
