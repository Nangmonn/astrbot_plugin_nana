"""Integration harness for the nana group guard plugin.

Stubs the AstrBot runtime (``astrbot.api``) so the plugin can be driven with
synthetic OneBot events, then asserts on the configured behaviour for join
requests, member verification, and failure handling.

Usage:
    python tools/harness.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_FILE = PLUGIN_DIR / "main.py"

LOGGER = logging.getLogger("harness")


# ──────────────────────────────────────────────────────────────
# AstrBot stubs
# ──────────────────────────────────────────────────────────────


def _install_astrbot_stubs() -> None:
    """Register minimal astrbot modules so ``main.py`` can be imported."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    astrbot.api = api

    class Plain:
        def __init__(self, text: str) -> None:
            self.text = text

    class At:
        def __init__(self, qq: Any) -> None:
            self.qq = qq

    components = ModuleType("astrbot.api.message_components")
    components.Plain = Plain
    components.At = At
    api.message_components = components

    class AstrMessageEvent:
        def __init__(
            self,
            raw: dict,
            *,
            self_id: str = "10000",
            bot: Any = None,
            admins: set[str] | None = None,
        ) -> None:
            self.message_obj = SimpleNamespace(raw_message=raw)
            self._self_id = self_id
            self.bot = bot
            self._admins = admins or set()

        def get_platform_name(self) -> str:
            return "aiocqhttp"

        def get_group_id(self) -> str:
            return str(self.message_obj.raw_message.get("group_id") or "")

        def get_sender_id(self) -> str:
            return str(self.message_obj.raw_message.get("user_id") or "")

        def get_self_id(self) -> str:
            return self._self_id

        @property
        def unified_msg_origin(self) -> str:
            return f"aiocqhttp:GroupMessage:{self.get_group_id()}"

        @property
        def message_str(self) -> str:
            return str(self.message_obj.raw_message.get("message") or "")

        def get_messages(self) -> list[Any]:
            return [Plain(self.message_str)]

        def is_admin(self) -> bool:
            return self.get_sender_id() in self._admins

        def plain_result(self, text: str) -> Any:
            return ("plain", text)

    class EventMessageType:
        ALL = "ALL"
        GROUP_MESSAGE = "GROUP"

    def _decorator_factory(*_args: Any, **_kwargs: Any):
        def wrap(func):
            return func

        return wrap

    filter_mod = ModuleType("astrbot.api.event.filter")
    filter_mod.EventMessageType = EventMessageType
    filter_mod.event_message_type = _decorator_factory
    filter_mod.command = _decorator_factory

    event_mod = ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = filter_mod
    event_mod.MessageChain = object

    class Star:
        def __init__(self, context: Any, config: dict | None = None) -> None:
            self.context = context

        async def initialize(self) -> None:  # pragma: no cover - overridden
            pass

        async def terminate(self) -> None:  # pragma: no cover - overridden
            pass

    class StarTools:
        data_root = PLUGIN_DIR / ".harness_data"

        @classmethod
        def get_data_dir(cls, plugin_name: str | None = None) -> Path:
            path = cls.data_root / (plugin_name or "unknown")
            path.mkdir(parents=True, exist_ok=True)
            return path

    star_mod = ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = _decorator_factory

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event_mod,
            "astrbot.api.event.filter": filter_mod,
            "astrbot.api.star": star_mod,
        }
    )


_install_astrbot_stubs()
from astrbot.api.event import AstrMessageEvent  # noqa: E402  (stub)
from astrbot.api.star import StarTools  # noqa: E402  (stub)


def load_plugin_module() -> ModuleType:
    """Import the plugin module from its file path."""
    spec = importlib.util.spec_from_file_location("nana_plugin_under_test", PLUGIN_FILE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ──────────────────────────────────────────────────────────────
# Fake bot / context
# ──────────────────────────────────────────────────────────────


class FakeApi:
    def __init__(self, owner: "FakeBot") -> None:
        self.owner = owner

    async def call_action(self, action: str, **kwargs: Any) -> Any:
        self.owner.calls.append((action, kwargs))
        if action in self.owner.fail_actions:
            raise RuntimeError(f"api {action} failed (stubbed)")
        if action == "get_group_member_list":
            return self.owner.members
        if action == "get_group_member_info":
            info = self.owner.members_info.get(str(kwargs.get("user_id")))
            if info is None:
                # 真实 OneBot 实现对不在群的成员会返回错误而不是空值
                raise RuntimeError("user not in group (stubbed)")
            return info
        if action == "get_stranger_info":
            return self.owner.stranger_info.get(
                str(kwargs.get("user_id")), {"nickname": "路人"}
            )
        if action == "get_group_info":
            return {"group_name": "测试群"}
        return {"status": "ok"}


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_actions: set[str] = set()
        self.members: list[dict] = []
        self.members_info: dict[str, dict] = {}
        self.stranger_info: dict[str, dict] = {}
        self.api = FakeApi(self)

    def calls_of(self, action: str) -> list[dict]:
        return [kwargs for name, kwargs in self.calls if name == action]


class FakeProvider:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def text_chat(self, prompt: str | None = None, **_kwargs: Any) -> Any:
        self.prompts.append(prompt or "")
        return SimpleNamespace(completion_text=self.answer)


class FakeContext:
    def __init__(self, provider: FakeProvider | None = None) -> None:
        self.provider = provider

    async def get_using_provider_async(self, _umo: str | None = None) -> Any:
        return self.provider

    def get_provider_by_id(self, _provider_id: str) -> Any:
        return self.provider


# ──────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────


def default_config() -> dict:
    """Build the config the way AstrBot builds it from the schema."""
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))

    def parse(node: dict) -> dict:
        out: dict = {}
        for key, meta in node.items():
            if meta["type"] == "object":
                out[key] = parse(meta["items"])
            else:
                out[key] = meta.get("default")
        return out

    return parse(schema)


# ──────────────────────────────────────────────────────────────
# Assertions
# ──────────────────────────────────────────────────────────────


class Checker:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, label: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed += 1
            print(f"  PASS  {label}")
        else:
            self.failed.append(label)
            print(f"  FAIL  {label} {detail}")


# ──────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────


async def main() -> int:
    plugin_mod = load_plugin_module()
    checker = Checker()
    wait = 0.35

    # ── 1. join request: regex pass ───────────────────────────
    print("\n[1] 入群申请：附言匹配正则 → 不审批不拒绝")
    cfg = default_config()
    cfg["join_request_verification"]["answer_pattern"] = r"\w+\s*#\s*\d+"
    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20001,
            "group_id": 90001,
            "comment": "娜娜#123",
            "flag": "flag-pass",
        },
        bot=bot,
    )
    await plugin.on_event(event)
    checker.check(
        "匹配正则时未调用 set_group_add_request",
        not bot.calls_of("set_group_add_request"),
        str(bot.calls),
    )

    # ── 2. join request: regex fail → auto reject + reply ────
    print("\n[2] 入群申请：附言不匹配 → 自动审批拒绝 + 回复申请人")
    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20002,
            "group_id": 90001,
            "comment": "加我微信买号",
            "flag": "flag-reject",
        },
        bot=bot,
    )
    await plugin.on_event(event)
    rejects = bot.calls_of("set_group_add_request")
    checker.check("调用了 set_group_add_request", len(rejects) >= 1, str(bot.calls))
    if rejects:
        first = rejects[0]
        checker.check("approve=False", first.get("approve") is False, str(first))
        checker.check("sub_type=add", first.get("sub_type") == "add", str(first))
        checker.check("flag 正确透传", first.get("flag") == "flag-reject", str(first))
        checker.check(
            "拒绝理由来自配置", "格式不正确" in str(first.get("reason", "")), str(first)
        )
    privates = bot.calls_of("send_private_msg")
    checker.check("向申请人发送了拒绝回复", len(privates) >= 1, str(bot.calls))
    if privates:
        checker.check(
            "回复中包含拒绝原因",
            "格式" in str(privates[0].get("message", "")),
            str(privates[0]),
        )

    # ── 3. join request: AI adjudication ─────────────────────
    print("\n[3] 入群申请：AI 判定广告人机 → 拒绝")
    cfg_ai = default_config()
    cfg_ai["join_request_verification"].update(
        {"use_llm": True, "llm_mode": "正则不通过时", "answer_pattern": r"\w+\s*#\s*\d+"}
    )
    provider = FakeProvider("REJECT")
    bot = FakeBot()
    bot.stranger_info["20003"] = {"nickname": "小号", "level": 2}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(provider), cfg_ai)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20003,
            "group_id": 90001,
            "comment": "你好",
            "flag": "flag-ai",
        },
        bot=bot,
    )
    await plugin.on_event(event)
    checker.check("AI 判定被调用", len(provider.prompts) == 1, str(provider.prompts))
    if provider.prompts:
        checker.check(
            "提示词注入了等级",
            "等级：2" in provider.prompts[0],
            provider.prompts[0][:120],
        )
        checker.check(
            "提示词注入了附言",
            "申请附言：你好" in provider.prompts[0],
            provider.prompts[0][:120],
        )
    checker.check(
        "AI 判定后执行拒绝",
        len(bot.calls_of("set_group_add_request")) >= 1,
        str(bot.calls),
    )

    print("\n[4] 入群申请：AI 判定放行真实玩家 → 不拒绝")
    provider = FakeProvider("PASS")
    bot = FakeBot()
    bot.stranger_info["20004"] = {"nickname": "老玩家", "level": 42}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(provider), cfg_ai)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20004,
            "group_id": 90001,
            "comment": "娜娜#7",
            "flag": "flag-ai-pass",
        },
        bot=bot,
    )
    await plugin.on_event(event)
    checker.check(
        "AI 输出 PASS 时不调用拒绝接口",
        not bot.calls_of("set_group_add_request"),
        str(bot.calls),
    )

    print("\n[5] 入群申请：AI 超时 → 按正则结果兜底")
    cfg_fb = default_config()
    cfg_fb["join_request_verification"].update(
        {
            "use_llm": True,
            "llm_mode": "始终由AI判定",
            "llm_fallback": "按正则结果处理",
            "llm_timeout": 5,
        }
    )

    class SlowProvider(FakeProvider):
        async def text_chat(self, prompt: str | None = None, **_kwargs: Any) -> Any:
            await asyncio.sleep(10)
            return SimpleNamespace(completion_text="PASS")

    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(SlowProvider("")), cfg_fb)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20005,
            "group_id": 90001,
            "comment": "娜娜#99",
            "flag": "flag-timeout",
        },
        bot=bot,
    )
    # 把超时压到 0.1s 以便快速验证兜底分支
    cfg_fb["join_request_verification"]["llm_timeout"] = 0.1
    await plugin.on_event(event)
    checker.check(
        "AI 超时且正则匹配 → 不误杀",
        not bot.calls_of("set_group_add_request"),
        str(bot.calls),
    )

    print("\n[6] 入群申请：AI 输出解析与拉黑降级")
    parse_cases = [
        ("REJECT", False),
        ("PASS", True),
        ("放行", True),
        ("该申请应被拒绝，疑似广告号", False),
        ("结论：PASS。附言格式符合要求", True),
        ("我觉得不好说，无法判断", None),
    ]
    for text, expected in parse_cases:
        bot = FakeBot()
        plugin = plugin_mod.NanaGroupGuard(FakeContext(FakeProvider(text)), cfg_ai)
        verdict = await plugin._judge_with_llm(
            AstrMessageEvent({}, bot=bot),
            cfg_ai["global"],
            cfg_ai["join_request_verification"],
            group_id="90001",
            user_id="1",
            user_name="测试",
            level=10,
            comment="随便写",
        )
        checker.check(
            f"AI 输出「{text}」→ {expected}",
            verdict is expected,
            f"got {verdict!r}",
        )

    # 拉黑字段不被适配器支持时应降级为普通拒绝，且拒绝一定生效
    cfg_bl = default_config()
    cfg_bl["join_request_verification"].update(
        {"reject_blacklist": True, "answer_pattern": r"\w+#\d+"}
    )
    bot = FakeBot()
    bot.fail_actions = {"set_group_add_request"}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_bl)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "user_id": 20009,
                "group_id": 90001,
                "comment": "广告",
                "flag": "flag-bl",
            },
            bot=bot,
        )
    )
    checker.check(
        "拒绝接口整体异常时不会崩溃（已记录日志）",
        len(bot.calls_of("set_group_add_request")) >= 1,
        str(bot.calls),
    )

    print("\n[7] 入群申请：低等级直接拒绝")
    cfg_lv = default_config()
    cfg_lv["join_request_verification"]["reject_level_below"] = 8
    provider = FakeProvider("PASS")
    bot = FakeBot()
    bot.stranger_info["20006"] = {"nickname": "新号", "level": 3}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(provider), cfg_lv)
    event = AstrMessageEvent(
        {
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "user_id": 20006,
            "group_id": 90001,
            "comment": "娜娜#3",
            "flag": "flag-level",
        },
        bot=bot,
    )
    await plugin.on_event(event)
    checker.check(
        "等级不足时直接拒绝",
        len(bot.calls_of("set_group_add_request")) >= 1,
        str(bot.calls),
    )
    checker.check("低等级不调用 AI", not provider.prompts, str(provider.prompts))

    # ── 7. member verification: correct answer ───────────────
    print("\n[8] 入群后验证：答对 → 通过")
    cfg_m = default_config()
    cfg_m["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 3}
    )
    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_m)
    raw = {
        "post_type": "notice",
        "notice_type": "group_increase",
        "user_id": 30001,
        "group_id": 90002,
    }
    await plugin.on_event(AstrMessageEvent(raw, bot=bot))
    await asyncio.sleep(wait)
    questions = bot.calls_of("send_group_msg")
    checker.check("入群后发送了题目", len(questions) == 1, str(bot.calls))
    if questions:
        message = questions[0]["message"]
        checker.check("题目以 @成员 开头", isinstance(message, list), str(message))
    record = plugin.memory.get("90002:30001")
    checker.check("内存中已记录验证状态", record is not None, str(plugin.memory))
    if record:
        answer = int(record["answer"])
        await plugin.on_event(
            AstrMessageEvent(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": 30001,
                    "group_id": 90002,
                    "message": str(answer),
                },
                bot=bot,
            )
        )
        await asyncio.sleep(wait)
        checker.check(
            "答对后清理状态",
            "90002:30001" not in plugin.memory,
            str(plugin.memory.keys()),
        )
        texts = [
            json.dumps(kwargs.get("message"), ensure_ascii=False)
            for kwargs in bot.calls_of("send_group_msg")
        ]
        checker.check(
            "发送了成功回复",
            any("验证通过" in text for text in texts),
            str(texts),
        )

    # ── 8. member verification: wrong answers → kick ─────────
    print("\n[8] 入群后验证：连续答错 → 踢出")
    cfg_k = default_config()
    cfg_k["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 2}
    )
    cfg_k["fail_action"].update({"action": "踢出", "kick_blacklist": True})
    bot = FakeBot()
    bot.members_info["30002"] = {"nickname": "机器人", "card": ""}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_k)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30002,
                "group_id": 90003,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    for _ in range(2):
        record = plugin.memory.get("90003:30002") or {}
        wrong = int(record.get("answer", 0)) + 1
        await plugin.on_event(
            AstrMessageEvent(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": 30002,
                    "group_id": 90003,
                    "message": str(wrong),
                },
                bot=bot,
            )
        )
        await asyncio.sleep(0.8)
    kicks = bot.calls_of("set_group_kick")
    checker.check("达失败上限后踢出", len(kicks) == 1, str(bot.calls))
    if kicks:
        checker.check(
            "踢出时带拉黑标记",
            kicks[0].get("reject_add_request") is True,
            str(kicks[0]),
        )
    checker.check(
        "踢出后未禁言", not bot.calls_of("set_group_ban"), str(bot.calls)
    )
    checker.check("状态已清理", "90003:30002" not in plugin.memory, str(plugin.memory))

    # ── 9. member verification: timeout + non-number ─────────
    print("\n[9] 入群后验证：非数字不计次、超时计次 → 禁言")
    cfg_t = default_config()
    # verify_timeout 在插件内被下限钳制到 10 秒，这里用 11 秒验证超时分支
    cfg_t["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 11, "max_attempts": 1}
    )
    cfg_t["fail_action"].update({"action": "禁言", "mute_seconds": 120})
    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_t)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30003,
                "group_id": 90004,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    stat_before = plugin.memory.get("90004:30003")
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30003,
                "group_id": 90004,
                "message": "我不回答",
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.2)
    stat_after = plugin.memory.get("90004:30003")
    checker.check(
        "非数字内容不消耗次数",
        bool(stat_before)
        and bool(stat_after)
        and stat_after.get("attempts") == stat_before.get("attempts"),
        f"{stat_before} -> {stat_after}",
    )
    texts = [
        json.dumps(kwargs.get("message"), ensure_ascii=False)
        for kwargs in bot.calls_of("send_group_msg")
    ]
    checker.check(
        "发送了非数字提示", any("数字" in text for text in texts), str(texts)
    )
    await asyncio.sleep(11.3)
    bans = bot.calls_of("set_group_ban")
    checker.check("超时后执行禁言", len(bans) == 1, str(bot.calls))
    if bans:
        checker.check(
            "禁言时长来自配置",
            bans[0].get("duration") == 120,
            str(bans[0]),
        )

    # ── 10. secondary verification: cross-group notice ───────
    print("\n[10] 失败处理：二次验证 → 禁言 + 跨群通知 + 管理员指令")
    cfg_s = default_config()
    cfg_s["global"]["notify_group"] = "88888"
    cfg_s["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 1}
    )
    cfg_s["fail_action"].update(
        {"action": "二次验证", "mute_seconds": 600, "admin_decision_timeout": 0}
    )
    bot = FakeBot()
    bot.members_info["30004"] = {"nickname": "可疑号", "card": ""}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_s)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30004,
                "group_id": 90005,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin.memory.get("90005:30004") or {}).get("answer", 0))
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30004,
                "group_id": 90005,
                "message": str(answer + 1),
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.8)
    bans = bot.calls_of("set_group_ban")
    checker.check("二次验证先禁言原群成员", len(bans) == 1, str(bot.calls))
    if bans:
        checker.check("禁言对象正确", bans[0].get("user_id") == 30004, str(bans[0]))
    notices = [
        kwargs
        for kwargs in bot.calls_of("send_group_msg")
        if kwargs.get("group_id") == 88888
    ]
    checker.check("向通知群发送了提醒", len(notices) == 1, str(bot.calls))
    if notices:
        text = json.dumps(notices[0].get("message"), ensure_ascii=False)
        checker.check("提醒包含 qq: 前缀", "qq:30004" in text, text)
        checker.check("提醒包含群号", "90005" in text, text)
        checker.check("提醒包含处理指令提示", "通过验证" in text, text)
    pend = plugin.memory.get("90005:30004")
    checker.check(
        "生成了待处理二次验证记录",
        bool(pend) and pend.get("kind") == "secondary_pending",
        str(pend),
    )

    print("\n[11] 管理员在通知群执行 /通过验证 → 解除禁言")
    admin_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 70001,
            "group_id": 88888,
            "message": "/通过验证 30004",
        },
        bot=bot,
    )
    results = [item async for item in plugin.cmd_approve(admin_event)]
    unmutes = [
        kwargs
        for kwargs in bot.calls_of("set_group_ban")
        if kwargs.get("duration") == 0
    ]
    checker.check("通过验证后解除禁言", len(unmutes) == 1, str(bot.calls))
    if unmutes:
        checker.check("解除的是原群", unmutes[0].get("group_id") == 90005, str(unmutes[0]))
    checker.check("记录已移除", "90005:30004" not in plugin.memory, str(plugin.memory))
    checker.check("返回了处理结果", bool(results), str(results))

    print("\n[12] 管理员执行 /拒绝验证 → 踢出")
    plugin.config["global"]["notify_group"] = "88888"
    bot2 = FakeBot()
    plugin2 = plugin_mod.NanaGroupGuard(FakeContext(), cfg_s)
    await plugin2.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30005,
                "group_id": 90006,
            },
            bot=bot2,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin2.memory.get("90006:30005") or {}).get("answer", 0))
    await plugin2.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30005,
                "group_id": 90006,
                "message": str(answer + 1),
            },
            bot=bot2,
        )
    )
    await asyncio.sleep(0.8)
    reject_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 70001,
            "group_id": 88888,
            "message": "/拒绝验证 30005",
        },
        bot=bot2,
    )
    await anext_or_list(plugin2.cmd_reject(reject_event))
    kicks = bot2.calls_of("set_group_kick")
    checker.check("拒绝验证后踢出", len(kicks) == 1, str(bot2.calls))
    if kicks:
        checker.check("踢出原群成员", kicks[0].get("user_id") == 30005, str(kicks[0]))

    # ── 13. per-group override ───────────────────────────────
    print("\n[13] 群自定义配置覆盖全局默认")
    cfg_o = default_config()
    cfg_o["group_custom_configs"] = [
        {
            "group_id": "91000",
            "follow_default": False,
            "join_request_verification": {"answer_pattern": r"^\d{4,8}$"},
            "member_verification": {"enabled": False},
            "fail_action": {"action": "踢出", "mute_seconds": 30},
        }
    ]
    cfg_o["member_verification"]["enabled"] = True
    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_o)
    _, join_cfg, member_cfg, fail_cfg, _ = plugin._group_config("91000")
    checker.check(
        "群正则被覆盖",
        join_cfg.get("answer_pattern") == r"^\d{4,8}$",
        str(join_cfg.get("answer_pattern")),
    )
    checker.check("该群入群后验证被关闭", member_cfg.get("enabled") is False, str(member_cfg))
    checker.check("该群失败处理被覆盖", fail_cfg.get("action") == "踢出", str(fail_cfg))

    # 覆盖生效：附言 "娜娜#123" 在 91000 群应当被拒绝
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "user_id": 30010,
                "group_id": 91000,
                "comment": "娜娜#123",
                "flag": "flag-override",
            },
            bot=bot,
        )
    )
    checker.check(
        "覆盖后的正则对该群生效",
        len(bot.calls_of("set_group_add_request")) == 1,
        str(bot.calls),
    )
    # 该群已关闭入群后验证
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30011,
                "group_id": 91000,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    checker.check(
        "关闭后不再发送验证题",
        not bot.calls_of("send_group_msg"),
        str(bot.calls),
    )

    # ── 14. misc: question quality, admin skip, non-group events
    print("\n[14] 其它：出题范围、管理员跳过、群号校验、非目标事件")
    cfg_q = default_config()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_q)
    print("      默认难度样例：", ", ".join(plugin._make_question({})[0] for _ in range(5)))
    samples = [plugin._make_question(cfg_q["member_verification"]) for _ in range(400)]
    checker.check(
        "默认难度两加数与结果都在 10 以内",
        all(
            int(q.split()[0]) <= 10
            and int(q.split()[2]) <= 10
            and 0 <= a <= 10
            for q, a in samples
        ),
        str(samples[:5]),
    )
    checker.check("答案非负", all(a >= 0 for _, a in samples), str(samples[:3]))
    negatives = [
        plugin._make_question(
            {"difficulty": "简单（10以内加减）", "allow_subtract_negative": True}
        )
        for _ in range(300)
    ]
    checker.check(
        "允许负数时可出现负答案",
        any(a < 0 for _, a in negatives),
        str([a for _, a in negatives if a < 0][:5]),
    )
    checker.check(
        "算式与答案一致",
        all(eval(q.replace("×", "*")) == a for q, a in samples),  # noqa: S307
        str(samples[:3]),
    )

    bot = FakeBot()
    bot.members = [
        {"user_id": 40001, "role": "owner"},
        {"user_id": 40002, "role": "admin"},
    ]
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_q)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 40002,
                "group_id": 92000,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    checker.check(
        "管理员入群被跳过", not bot.calls_of("send_group_msg"), str(bot.calls)
    )

    bad_cfg = default_config()
    bad_cfg["global"]["group_custom_configs"] = [{"group_id": "abc", "follow_default": False}]
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), bad_cfg)
    checker.check("非数字群号被跳过", plugin._group_overrides() == {}, str(plugin._group_overrides()))

    builder = plugin_mod._safe_format
    checker.check(
        "未定义占位符保持原样",
        builder("你好 {unknown} 和 {user_id}", user_id="123") == "你好 {unknown} 和 123",
        builder("你好 {unknown} 和 {user_id}", user_id="123"),
    )

    bot = FakeBot()
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_q)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "poke",
                "user_id": 50001,
                "group_id": 93000,
            },
            bot=bot,
        )
    )
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "request",
                "request_type": "friend",
                "user_id": 50002,
                "comment": "求加好友",
                "flag": "f1",
            },
            bot=bot,
        )
    )
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 10000,
                "group_id": 93001,
            },
            bot=bot,
        )
    )
    checker.check("无关事件不产生任何调用", not bot.calls, str(bot.calls))

    # ── 15. unmute command + status command ──────────────────
    print("\n[15] 指令：/解除禁言 与 /群管状态")
    bot = FakeBot()
    bot.members = [{"user_id": 70002, "role": "admin"}]
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_q)
    unmute_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 70002,
            "group_id": 93002,
            "message": "/解除禁言 60001",
        },
        bot=bot,
    )
    out = await anext_or_list(plugin.cmd_unmute(unmute_event))
    checker.check(
        "管理员可解除禁言",
        any(
            kwargs.get("duration") == 0 and kwargs.get("user_id") == 60001
            for kwargs in bot.calls_of("set_group_ban")
        ),
        str(bot.calls),
    )
    checker.check("返回了解除结果", bool(out), str(out))

    stranger_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 99999,
            "group_id": 93002,
            "message": "/解除禁言 60002",
        },
        bot=bot,
    )
    before = len(bot.calls_of("set_group_ban"))
    out = await anext_or_list(plugin.cmd_unmute(stranger_event))
    # 鉴权会查询一次成员列表，但不应产生任何禁言操作或回复
    checker.check(
        "普通成员无权执行指令",
        not out and len(bot.calls_of("set_group_ban")) == before,
        f"out={out} bans={bot.calls_of('set_group_ban')[before:]}",
    )

    status_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 70002,
            "group_id": 93002,
            "message": "/群管状态",
        },
        bot=bot,
    )
    out = await anext_or_list(plugin.cmd_status(status_event))
    text = out[0][1] if out else ""
    checker.check("状态输出包含正则配置", "answer_pattern" in text or "#" in text, text[:200])
    checker.check("状态输出包含失败处理", "失败处理" in text, text[:200])

    # ── 16. state persistence ────────────────────────────────
    print("\n[16] 持久化：待处理记录写入磁盘并可恢复")
    state_path = StarTools.get_data_dir("astrbot_plugin_nana") / "pending_verifications.json"
    checker.check("状态文件已生成", state_path.exists(), str(state_path))
    if state_path.exists():
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        checker.check("状态文件为合法 JSON 对象", isinstance(saved, dict), str(type(saved)))
        checker.check(
            "状态文件中不含 future 对象",
            all("future" not in record for record in saved.values()),
            str(list(saved.values())[:2]),
        )

    # ── 17. failure of platform API does not crash ───────────
    print("\n[17] 健壮性：平台接口报错时不崩溃")
    cfg_e = default_config()
    cfg_e["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 1}
    )
    cfg_e["fail_action"].update({"action": "踢出"})
    bot = FakeBot()
    bot.fail_actions = {"get_group_member_list", "get_group_member_info"}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_e)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30007,
                "group_id": 94000,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    checker.check(
        "成员列表接口异常时仍会出题", len(bot.calls_of("send_group_msg")) == 1, str(bot.calls)
    )
    answer = int((plugin.memory.get("94000:30007") or {}).get("answer", 0))
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30007,
                "group_id": 94000,
                "message": str(answer + 1),
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.9)
    checker.check(
        "题库接口异常时仍能进入失败处理",
        len(bot.calls_of("set_group_kick")) == 1,
        str(bot.calls),
    )

    # ── 18. bot without admin rights ─────────────────────────
    print("\n[18] 机器人非管理员：跨群转发仍成功，但动作如实回报失败")
    cfg_np = default_config()
    cfg_np["global"]["notify_group"] = "88880"
    cfg_np["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 1}
    )
    cfg_np["fail_action"].update(
        {"action": "二次验证", "mute_seconds": 600, "admin_decision_timeout": 0}
    )
    bot = FakeBot()
    # 机器人自身角色为普通成员；禁言/踢出接口会被平台拒绝
    bot.members_info["10000"] = {"nickname": "娜娜", "role": "member"}
    bot.fail_actions = {"set_group_ban", "set_group_kick"}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_np)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30020,
                "group_id": 95000,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin.memory.get("95000:30020") or {}).get("answer", 0))
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30020,
                "group_id": 95000,
                "message": str(answer + 1),
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.8)

    notices = [
        kwargs
        for kwargs in bot.calls_of("send_group_msg")
        if kwargs.get("group_id") == 88880
    ]
    checker.check("非管理员时跨群转发依然成功", len(notices) == 1, str(bot.calls))
    if notices:
        text = json.dumps(notices[0].get("message"), ensure_ascii=False)
        checker.check("通知中标明原群动作未执行", "非本群管理员" in text, text)
        checker.check("通知中带出机器人角色", "member" in text, text)
    group_texts = [
        json.dumps(kwargs.get("message"), ensure_ascii=False)
        for kwargs in bot.calls_of("send_group_msg")
        if kwargs.get("group_id") == 95000
    ]
    checker.check(
        "原群不再谎称已禁言",
        not any("已被禁言" in text for text in group_texts)
        and any("无权限禁言" in text for text in group_texts),
        str(group_texts),
    )
    checker.check(
        "状态记录如实标记未禁言成功",
        (plugin.memory.get("95000:30020") or {}).get("muted") is False,
        str(plugin.memory.get("95000:30020")),
    )

    print("\n[19] 机器人非管理员：/通过验证 与 /拒绝验证 如实报错")
    admin_event = AstrMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 70010,
            "group_id": 88880,
            "message": "/通过验证 30020",
        },
        bot=bot,
    )
    out = await anext_or_list(plugin.cmd_approve(admin_event))
    reply = out[0][1] if out else ""
    checker.check(
        "通过验证失败时回复明确报错",
        "解除禁言失败" in reply and "已标记通过" in reply,
        reply,
    )
    checker.check("记录仍被清理", "95000:30020" not in plugin.memory, str(plugin.memory))

    # 拒绝验证同样要如实报错
    bot2 = FakeBot()
    bot2.members_info["10000"] = {"nickname": "娜娜", "role": "member"}
    bot2.fail_actions = {"set_group_kick"}
    plugin2 = plugin_mod.NanaGroupGuard(FakeContext(), cfg_np)
    await plugin2.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30021,
                "group_id": 95001,
            },
            bot=bot2,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin2.memory.get("95001:30021") or {}).get("answer", 0))
    await plugin2.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30021,
                "group_id": 95001,
                "message": str(answer + 1),
            },
            bot=bot2,
        )
    )
    await asyncio.sleep(0.8)
    out = await anext_or_list(
        plugin2.cmd_reject(
            AstrMessageEvent(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": 70010,
                    "group_id": 88880,
                    "message": "/拒绝验证 30021",
                },
                bot=bot2,
            )
        )
    )
    reply = out[0][1] if out else ""
    checker.check(
        "拒绝验证失败时回复明确报错",
        "移出失败" in reply and "已标记拒绝" in reply,
        reply,
    )

    print("\n[20] 机器人非管理员：禁言/踢出模式同样如实提示")
    cfg_kick = default_config()
    cfg_kick["member_verification"].update(
        {"join_delay": 0, "verify_timeout": 5, "max_attempts": 1}
    )
    cfg_kick["fail_action"].update({"action": "踢出"})
    bot = FakeBot()
    bot.members_info["10000"] = {"nickname": "娜娜", "role": "member"}
    bot.fail_actions = {"set_group_kick"}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_kick)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30022,
                "group_id": 95002,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin.memory.get("95002:30022") or {}).get("answer", 0))
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30022,
                "group_id": 95002,
                "message": str(answer + 1),
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.9)
    texts = [
        json.dumps(kwargs.get("message"), ensure_ascii=False)
        for kwargs in bot.calls_of("send_group_msg")
        if kwargs.get("group_id") == 95002
    ]
    checker.check(
        "踢出失败时在原群明确提示管理员手动处理",
        any("无权限移出" in text for text in texts),
        str(texts),
    )

    print("\n[21] 机器人有管理员权限时行为不变")
    bot = FakeBot()
    bot.members_info["10000"] = {"nickname": "娜娜", "role": "admin"}
    plugin = plugin_mod.NanaGroupGuard(FakeContext(), cfg_np)
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "user_id": 30023,
                "group_id": 95003,
            },
            bot=bot,
        )
    )
    await asyncio.sleep(wait)
    answer = int((plugin.memory.get("95003:30023") or {}).get("answer", 0))
    await plugin.on_event(
        AstrMessageEvent(
            {
                "post_type": "message",
                "message_type": "group",
                "user_id": 30023,
                "group_id": 95003,
                "message": str(answer + 1),
            },
            bot=bot,
        )
    )
    await asyncio.sleep(0.8)
    notices = [
        kwargs
        for kwargs in bot.calls_of("send_group_msg")
        if kwargs.get("group_id") == 88880
    ]
    checker.check(
        "有权限时通知中标明已禁言",
        bool(notices)
        and "已禁言" in json.dumps(notices[0].get("message"), ensure_ascii=False),
        str(notices),
    )
    out = await anext_or_list(
        plugin.cmd_approve(
            AstrMessageEvent(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "user_id": 70010,
                    "group_id": 88880,
                    "message": "/通过验证 30023",
                },
                bot=bot,
            )
        )
    )
    reply = out[0][1] if out else ""
    checker.check("有权限时通过验证成功", "解除禁言" in reply and "失败" not in reply, reply)

    await plugin.terminate()

    print(f"\n{'=' * 52}")
    print(f"通过 {checker.passed} 项，失败 {len(checker.failed)} 项")
    for label in checker.failed:
        print(f"  - {label}")
    return 1 if checker.failed else 0


async def anext_or_list(agen: Any) -> list[Any]:
    """Collect every item from an async generator without raising StopAsyncIteration."""
    out: list[Any] = []
    async for item in agen:
        out.append(item)
    return out


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
