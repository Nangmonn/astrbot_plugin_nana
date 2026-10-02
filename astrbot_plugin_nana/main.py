"""AstrBot 娜娜群管插件。

提供两段式入群风控：

1. 入群申请验证：对 ``request.group.add`` 事件的附言做正则匹配，可选交给大模型
   结合 QQ 等级与留言判断是真实玩家还是广告人机，不通过则自动审批拒绝并回复申请人。
2. 入群后算术验证：对 ``notice.group_increase`` 事件发送限时加减法，成员在规定时间与
   次数内答错用尽机会后，按配置执行禁言 / 踢出 / 二次验证（禁言并到通知群提醒管理员）。

所有阈值、文案与正则均通过 ``_conf_schema.json`` 暴露给 WebUI，并支持按群覆盖。
"""

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools, register

PLUGIN_NAME = "astrbot_plugin_nana"
SUPPORTED_PLATFORM = "aiocqhttp"
STATE_FILE = "pending_verifications.json"

# 内存中同时保留的验证记录上限，避免状态无限增长。
MAX_PENDING_RECORDS = 200


def _raw_field(raw: Any, key: str, default: Any = None) -> Any:
    """从 OneBot 原始事件中读取字段。

    aiocqhttp 适配器把原始事件以 ``aiocqhttp.Event`` 对象挂在
    ``message_obj.raw_message`` 上，而部分适配器会给出普通 dict，
    因此这里同时兼容两种形态。

    Args:
        raw: 原始事件对象或字典。
        key: 要读取的字段名。
        default: 字段缺失时的默认值。

    Returns:
        字段值，缺失时返回 ``default``。
    """
    if raw is None:
        return default
    try:
        value = raw.get(key)
    except AttributeError:
        try:
            value = raw[key]
        except Exception:
            return default
    except Exception:
        return default
    return default if value is None else value


def _safe_format(template: Any, **kwargs: Any) -> str:
    """安全格式化文案，未定义的占位符保持原样而不抛异常。

    Args:
        template: 含 ``{name}`` 占位符的文案。
        **kwargs: 占位符取值。

    Returns:
        格式化后的字符串；模板异常时返回其字符串形式。
    """

    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    if not isinstance(template, str):
        return str(template)
    try:
        return template.format_map(_SafeDict(**kwargs))
    except Exception:
        return template


def _to_int(value: Any, default: int) -> int:
    """把配置值安全地转换为整数。

    Args:
        value: 原始配置值，可能来自 WebUI 的字符串输入。
        default: 转换失败时的兜底值。

    Returns:
        转换后的整数。
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _mask(text: Any, width: int = 30) -> str:
    """压缩文本长度，保证日志宽度可控。

    Args:
        text: 原始文本。
        width: 最大保留字符数。

    Returns:
        截断后的单行文本。
    """
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= width else text[:width] + "…"


class PendingStore:
    """验证状态存储。

    内存字典是唯一事实来源，保证并发读写一致；每次变更做一次尽力而为的落盘，
    用于插件重载后恢复尚未处理完的二次验证记录。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.path = data_dir / STATE_FILE
        self.lock = asyncio.Lock()
        self.memory: dict[str, dict] = {}

    def load(self) -> dict[str, dict]:
        """从磁盘读取状态文件。

        Returns:
            反序列化后的状态字典；文件缺失或损坏时返回空字典。
        """
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[娜娜群管] 读取状态文件失败，已忽略: {exc}")
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        """把内存状态写入磁盘，写入失败只记录日志。"""
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                key: {k: v for k, v in record.items() if k != "future"}
                for key, record in self.memory.items()
            }
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"[娜娜群管] 写入状态文件失败: {exc}")


@register(
    PLUGIN_NAME,
    "Nanmo",
    "娜娜群管：入群申请正则/AI 验证 + 入群后算术验证，支持禁言/踢出/二次验证",
    "1.2.0",
    repo="https://github.com/Nangmonn/astrbot_plugin_nana",
)
class NanaGroupGuard(Star):
    """群管验证插件主类。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config: dict = config if isinstance(config, dict) else {}
        # 运行期状态：内存为唯一事实来源，磁盘仅用于重载后恢复
        self.store = PendingStore(StarTools.get_data_dir(PLUGIN_NAME))
        self.memory = self.store.memory
        self.lock = self.store.lock
        # 后台任务句柄，插件卸载时统一取消
        self.verify_tasks: dict[str, asyncio.Task] = {}
        self.decision_tasks: dict[str, asyncio.Task] = {}

    # ══════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════

    async def initialize(self) -> None:
        """插件启用时载入持久化状态。"""
        restored = self.store.load()
        self.memory.update(restored)
        if restored:
            logger.info(f"[娜娜群管] 从磁盘恢复 {len(restored)} 条未完成记录")

    async def terminate(self) -> None:
        """插件停用或重载时取消后台任务并落盘。"""
        for task in [*self.verify_tasks.values(), *self.decision_tasks.values()]:
            if not task.done():
                task.cancel()
        self.verify_tasks.clear()
        self.decision_tasks.clear()
        for record in self.memory.values():
            future = record.get("future")
            if future is not None and not future.done():
                future.cancel()
            record.pop("future", None)
        self.store.save()
        logger.info("[娜娜群管] 插件已卸载，后台任务已清理")

    # ══════════════════════════════════════════════
    # 配置读取
    # ══════════════════════════════════════════════

    def _group_overrides(self) -> dict[str, dict]:
        """把 template_list 形式的群自定义配置转换成以群号为键的字典。

        Returns:
            ``{群号: 该群配置项}``；非法条目会被跳过。
        """
        items = self.config.get("group_custom_configs", [])
        result: dict[str, dict] = {}
        if not isinstance(items, list):
            return result
        for item in items:
            if not isinstance(item, dict):
                continue
            group_id = str(item.get("group_id", "")).strip()
            if not group_id:
                continue
            if not group_id.isdigit():
                logger.warning(
                    f"[娜娜群管] 群号 '{group_id}' 非纯数字，该条自定义配置已跳过"
                )
                continue
            if item.get("follow_default"):
                continue
            result[group_id] = item
        return result

    def _group_config(self, group_id: Any) -> tuple[dict, dict, dict, dict, str]:
        """合并全局默认与群自定义配置。

        Args:
            group_id: 群号。

        Returns:
            ``(全局设置, 入群申请验证配置, 入群后验证配置, 失败处理配置, 生效通知群号)``。
        """
        settings = self.config.get("global", {})
        settings = settings if isinstance(settings, dict) else {}
        join_cfg = self.config.get("join_request_verification", {})
        join_cfg = dict(join_cfg) if isinstance(join_cfg, dict) else {}
        member_cfg = self.config.get("member_verification", {})
        member_cfg = dict(member_cfg) if isinstance(member_cfg, dict) else {}
        fail_cfg = self.config.get("fail_action", {})
        fail_cfg = dict(fail_cfg) if isinstance(fail_cfg, dict) else {}

        override = self._group_overrides().get(str(group_id))
        if override:
            for section_name, target in (
                ("join_request_verification", join_cfg),
                ("member_verification", member_cfg),
                ("fail_action", fail_cfg),
            ):
                section = override.get(section_name)
                if isinstance(section, dict):
                    target.update(section)

        notify_group = str(fail_cfg.get("notify_group_override", "")).strip()
        if not notify_group:
            notify_group = str(settings.get("notify_group", "")).strip()
        return settings, join_cfg, member_cfg, fail_cfg, notify_group

    # ══════════════════════════════════════════════
    # 消息入口
    # ══════════════════════════════════════════════

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_event(self, event: AstrMessageEvent) -> None:
        """接收全部事件并按类型分发。

        Args:
            event: AstrBot 消息事件。
        """
        if event.get_platform_name() != SUPPORTED_PLATFORM:
            return
        settings = self.config.get("global", {})
        if isinstance(settings, dict) and not settings.get("enabled", True):
            return

        raw = event.message_obj.raw_message if event.message_obj else None
        if raw is None:
            return

        post_type = str(_raw_field(raw, "post_type", ""))

        if post_type == "request" and _raw_field(raw, "request_type") == "group":
            if _raw_field(raw, "sub_type") == "add":
                await self._handle_join_request(event, raw)
            return

        if post_type == "notice" and _raw_field(raw, "notice_type") == "group_increase":
            user_id = str(_raw_field(raw, "user_id", ""))
            group_id = str(_raw_field(raw, "group_id", ""))
            if not user_id or not group_id:
                return
            if user_id == str(event.get_self_id()):
                return
            await self._handle_group_increase(event, user_id, group_id)
            return

        if post_type == "message" and _raw_field(raw, "message_type") == "group":
            if event.message_str.strip().startswith("/"):
                # 指令消息交给 @filter.command 处理，避免与答案判定冲突
                return
            await self._handle_group_message(event)

    # ══════════════════════════════════════════════
    # 一、入群申请验证
    # ══════════════════════════════════════════════

    async def _handle_join_request(self, event: AstrMessageEvent, raw: Any) -> None:
        """处理入群申请事件：正则 / AI 判定后自动审批。

        Args:
            event: AstrBot 消息事件。
            raw: OneBot 原始 request 事件。
        """
        user_id = str(_raw_field(raw, "user_id", ""))
        group_id = str(_raw_field(raw, "group_id", ""))
        comment = str(_raw_field(raw, "comment", "") or "").strip()
        if not user_id or not group_id or not _raw_field(raw, "flag", ""):
            logger.warning("[娜娜群管] 入群申请事件缺少 user_id/group_id/flag，已忽略")
            return

        settings, join_cfg, _, _, _ = self._group_config(group_id)
        if not join_cfg.get("enabled", True):
            return

        user_name, level = await self._applicant_profile(event, user_id, raw)

        # 低等级直接拒绝，不进入正则与 AI 判定
        threshold = _to_int(join_cfg.get("reject_level_below", 0), 0)
        if threshold > 0 and isinstance(level, int) and level < threshold:
            await self._reject_join_request(
                event,
                raw,
                settings,
                join_cfg,
                group_id=group_id,
                user_id=user_id,
                user_name=user_name,
                comment=comment,
                reason=f"QQ 等级 {level} 低于本群入群门槛 {threshold}",
            )
            return

        pattern_ok, detail = self._match_answer_pattern(comment, join_cfg)
        logger.info(
            f"[娜娜群管] 入群申请 {user_name}({user_id}) → 群 {group_id}，"
            f"附言「{_mask(comment)}」正则结果={pattern_ok}（{detail}）"
        )

        use_llm = bool(join_cfg.get("use_llm", False))
        llm_mode = str(join_cfg.get("llm_mode", "正则不通过时"))
        decision: bool | None = None

        needs_llm = use_llm and (
            llm_mode == "仅AI判定"
            or llm_mode == "始终由AI判定"
            or not pattern_ok
        )
        if needs_llm:
            decision = await self._judge_with_llm(
                event,
                settings,
                join_cfg,
                group_id=group_id,
                user_id=user_id,
                user_name=user_name,
                level=level,
                comment=comment,
            )
            if decision is None:
                fallback = str(join_cfg.get("llm_fallback", "按正则结果处理"))
                if fallback == "拒绝":
                    decision = False
                elif fallback == "放行":
                    decision = True
                else:
                    decision = pattern_ok
                logger.info(f"[娜娜群管] AI 判定不可用，按兜底策略处理：{'放行' if decision else '拒绝'}")

        if decision is None:
            # 未启用 AI 判定时完全依赖正则
            decision = pattern_ok

        if decision:
            logger.info(
                f"[娜娜群管] 通过入群申请 {user_name}({user_id}) → 群 {group_id}"
                f"（{'AI 判定' if needs_llm else '正则匹配'}）"
            )
            return

        reason = (
            "AI 判定为广告或异常账号"
            if needs_llm and pattern_ok
            else f"申请附言不符合要求（{detail}）"
        )
        await self._reject_join_request(
            event,
            raw,
            settings,
            join_cfg,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            comment=comment,
            reason=reason,
        )

    def _match_answer_pattern(self, comment: str, join_cfg: dict) -> tuple[bool, str]:
        """用配置的正则全匹配入群附言。

        Args:
            comment: 申请人填写的附言。
            join_cfg: 入群申请验证配置。

        Returns:
            ``(是否通过, 说明文本)``。
        """
        pattern = str(join_cfg.get("answer_pattern", "")).strip()
        if not pattern:
            return True, "未配置正则，跳过正则校验"
        flags = re.IGNORECASE if join_cfg.get("pattern_ignore_case", True) else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            logger.error(f"[娜娜群管] 答案正则无法编译: {exc}")
            return False, f"正则配置错误：{exc}"
        if not comment:
            return False, "申请附言为空"
        if compiled.fullmatch(comment):
            return True, f"匹配 {pattern}"
        return False, f"不匹配 {pattern}"

    async def _judge_with_llm(
        self,
        event: AstrMessageEvent,
        settings: dict,
        join_cfg: dict,
        *,
        group_id: str,
        user_id: str,
        user_name: str,
        level: Any,
        comment: str,
    ) -> bool | None:
        """调用大模型综合等级与附言判断申请人是否放行。

        Args:
            event: AstrBot 消息事件。
            settings: 全局设置。
            join_cfg: 入群申请验证配置。
            group_id: 群号。
            user_id: 申请人 QQ 号。
            user_name: 申请人昵称。
            level: QQ 等级，未知时为字符串。
            comment: 申请附言。

        Returns:
            ``True`` 放行、``False`` 拒绝、``None`` 表示判定失败需走兜底策略。
        """
        provider = None
        provider_id = str(settings.get("default_provider_id", "")).strip()
        try:
            if provider_id:
                provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                provider = await self.context.get_using_provider_async(
                    event.unified_msg_origin
                )
        except Exception as exc:
            logger.warning(f"[娜娜群管] 获取 AI 模型失败: {exc}")
            return None
        if provider is None:
            logger.warning("[娜娜群管] 未找到可用的对话模型，AI 判定跳过")
            return None

        prompt = _safe_format(
            join_cfg.get("llm_prompt", ""),
            user_id=user_id,
            user_name=user_name,
            level=level,
            answer=comment or "（空）",
            comment=comment or "（空）",
            group_id=group_id,
            group_name=await self._group_name(event, group_id),
        )
        timeout = _to_int(join_cfg.get("llm_timeout", 20), 20)
        started = time.monotonic()
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是一个严格的群聊入群审核助手。",
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[娜娜群管] AI 判定超时（{timeout}s），按兜底策略处理")
            return None
        except Exception as exc:
            logger.warning(f"[娜娜群管] AI 判定调用失败: {exc}")
            return None

        text = (getattr(response, "completion_text", "") or "").strip()
        logger.info(
            f"[娜娜群管] AI 判定耗时 {time.monotonic() - started:.1f}s，输出「{_mask(text, 40)}」"
        )
        # 优先识别提示词要求的英文结论词；只有在没有英文结论时才退回中文关键词
        for pattern, decision in (
            (r"\bREJECT\b|\bREJECTED\b", False),
            (r"\bPASS\b|\bAPPROVE\b|\bAPPROVED\b", True),
        ):
            if re.search(pattern, text, re.IGNORECASE):
                return decision
        for pattern, decision in (
            (r"拒绝|不通过|不予通过|广告", False),
            (r"放行|通过", True),
        ):
            if re.search(pattern, text):
                logger.info("[娜娜群管] AI 未按格式输出英文结论，已按中文关键词判定")
                return decision
        logger.warning(f"[娜娜群管] AI 输出无法识别，按兜底策略处理：{_mask(text, 40)}")
        return None

    async def _reject_join_request(
        self,
        event: AstrMessageEvent,
        raw: Any,
        settings: dict,
        join_cfg: dict,
        *,
        group_id: str,
        user_id: str,
        user_name: str,
        comment: str,
        reason: str,
    ) -> None:
        """自动审批拒绝入群申请并按配置回复申请人。

        Args:
            event: AstrBot 消息事件。
            raw: OneBot 原始 request 事件。
            settings: 全局设置。
            join_cfg: 入群申请验证配置。
            group_id: 群号。
            user_id: 申请人 QQ 号。
            user_name: 申请人昵称。
            comment: 申请附言。
            reason: 拒绝原因，会填入回复文案的 ``{reason}``。
        """
        reject_reason = _safe_format(
            join_cfg.get("reject_reason", ""),
            reason=reason,
            user_name=user_name,
            user_id=user_id,
        )
        flag = str(_raw_field(raw, "flag", ""))
        payload: dict[str, Any] = {
            "flag": flag,
            "sub_type": "add",
            "approve": False,
            "reason": reject_reason,
        }
        if join_cfg.get("reject_blacklist", False):
            # OneBot 的"拒绝并拉黑"由 set_group_add_request 的 reject_add_request 表达，
            # 不同实现对该字段的支持不一致，不支持时会在下方自动降级重试。
            payload["reject_add_request"] = True
        try:
            await event.bot.api.call_action("set_group_add_request", **payload)
            logger.info(
                f"[娜娜群管] 已自动拒绝 {user_name}({user_id}) 加入群 {group_id}：{reason}"
            )
        except Exception as exc:
            logger.error(f"[娜娜群管] 自动拒绝入群申请失败: {exc}")
            if "reject_add_request" in payload:
                # 适配器不支持拉黑字段时，去掉该字段保证拒绝本身一定生效
                payload.pop("reject_add_request")
                try:
                    await event.bot.api.call_action("set_group_add_request", **payload)
                    logger.info(
                        f"[娜娜群管] 已拒绝 {user_name}({user_id})（适配器不支持拉黑，仅拒绝）"
                    )
                except Exception as exc2:
                    logger.error(f"[娜娜群管] 降级拒绝同样失败: {exc2}")

        reply = _safe_format(
            join_cfg.get("reject_reply", ""),
            user_name=user_name,
            user_id=user_id,
            reason=reason,
            group_id=group_id,
            answer=comment or "（空）",
        ).strip()
        if not reply:
            return
        if join_cfg.get("reject_private", True):
            if await self._send_private(event, user_id, reply):
                return
        # 未开启私聊、或陌生人私聊被平台拦截时退化为日志，拒绝动作本身已经完成
        logger.info(f"[娜娜群管] 拒绝回复未能私聊送达：{_mask(reply, 160)}")

    # ══════════════════════════════════════════════
    # 二、入群后算术验证
    # ══════════════════════════════════════════════

    async def _handle_group_increase(
        self, event: AstrMessageEvent, user_id: str, group_id: str
    ) -> None:
        """新成员入群：启动算术验证流程。

        Args:
            event: AstrBot 消息事件。
            user_id: 新成员 QQ 号。
            group_id: 群号。
        """
        _, _, member_cfg, _, _ = self._group_config(group_id)
        if not member_cfg.get("enabled", True):
            return

        if member_cfg.get("skip_admins", True):
            owner, admins = await self._group_owner_and_admins(event, group_id)
            if user_id == owner or user_id in admins:
                logger.info(f"[娜娜群管] {user_id} 是群主/管理员，跳过入群验证")
                return

        user_name = await self._display_name(event, group_id, user_id)

        # 同一成员在多个群同时验证时，用 group:user 作键互不干扰
        key = f"{group_id}:{user_id}"
        old = self.verify_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()

        task = asyncio.create_task(
            self._run_member_verification(event, key, group_id, user_id, user_name)
        )
        task.set_name(f"nana_verify_{group_id}_{user_id}")
        self.verify_tasks[key] = task
        task.add_done_callback(
            lambda t, k=key: self._on_task_done(t, self.verify_tasks, k)
        )

    async def _run_member_verification(
        self,
        event: AstrMessageEvent,
        key: str,
        group_id: str,
        user_id: str,
        user_name: str,
    ) -> None:
        """执行完整的算术验证循环。

        Args:
            event: AstrBot 消息事件。
            key: ``群号:QQ号`` 形式的验证键。
            group_id: 群号。
            user_id: 新成员 QQ 号。
            user_name: 新成员昵称。
        """
        _, _, member_cfg, fail_cfg, _ = self._group_config(group_id)
        delay = _to_int(member_cfg.get("join_delay", 3), 3)
        if delay > 0:
            await asyncio.sleep(delay)

        max_attempts = max(1, _to_int(member_cfg.get("max_attempts", 3), 3))
        timeout = max(10, _to_int(member_cfg.get("verify_timeout", 120), 120))
        at_member = bool(member_cfg.get("at_member", True))
        count_non_number = bool(member_cfg.get("count_non_number", False))

        attempts = 0
        last_question = ""
        last_answer = "（未回答）"
        last_timed_out = False
        # 已经发送给成员、正在等待作答的题目；失败提示会把下一题一并发出
        current: tuple[str, int] | None = None

        try:
            while attempts < max_attempts:
                remaining = max_attempts - attempts

                if current is None:
                    question, answer = self._make_question(member_cfg)
                    text = _safe_format(
                        member_cfg.get("question_format", ""),
                        question=question,
                        timeout=timeout,
                        max_attempts=max_attempts,
                        remaining=remaining,
                        attempts=attempts,
                        user_name=user_name,
                        user_id=user_id,
                    )
                    if not await self._send_group(
                        event, group_id, text, at_member, user_id
                    ):
                        logger.warning(
                            f"[娜娜群管] 无法向群 {group_id} 发送验证题目，终止对 {user_id} 的验证"
                        )
                        return
                else:
                    question, answer = current
                current = None
                last_question = question

                future: asyncio.Future = asyncio.get_running_loop().create_future()
                async with self.lock:
                    self._prune_memory()
                    self.memory[key] = {
                        "kind": "member_verify",
                        "group_id": group_id,
                        "user_id": user_id,
                        "user_name": user_name,
                        "attempts": attempts,
                        "max_attempts": max_attempts,
                        "question": question,
                        "answer": answer,
                        "last_answer": last_answer,
                        "created_at": time.time(),
                        "future": future,
                    }
                self.store.save()

                try:
                    outcome = await asyncio.wait_for(asyncio.shield(future), timeout)
                except asyncio.TimeoutError:
                    outcome = "timeout"
                except asyncio.CancelledError:
                    raise
                finally:
                    async with self.lock:
                        record = self.memory.get(key)
                        if record is not None:
                            last_answer = str(record.get("last_answer", last_answer))
                            record.pop("future", None)

                if outcome == "correct":
                    await self._finish_member_verification_ok(
                        event, group_id, user_id, user_name, member_cfg
                    )
                    return

                if outcome == "non_number" and not count_non_number:
                    # 非数字内容默认不消耗次数，只做提示并重发当前题面
                    hint = _safe_format(
                        member_cfg.get("non_number_reply", ""),
                        question=question,
                        remaining=remaining,
                        attempts=attempts,
                        max_attempts=max_attempts,
                    ).strip()
                    body = f"{hint}\n{question} = ?" if hint else f"{question} = ?"
                    if not await self._send_group(
                        event, group_id, body, at_member, user_id
                    ):
                        return
                    current = (question, answer)
                    continue

                attempts += 1
                last_timed_out = outcome == "timeout"
                remaining = max_attempts - attempts
                if remaining <= 0:
                    break

                next_question, next_answer = self._make_question(member_cfg)
                template = (
                    member_cfg.get("timeout_reply")
                    if last_timed_out
                    else member_cfg.get("wrong_reply")
                )
                prompt = _safe_format(
                    template,
                    remaining=remaining,
                    question=next_question,
                    attempts=attempts,
                    max_attempts=max_attempts,
                    user_name=user_name,
                    user_id=user_id,
                ).strip()
                if not prompt:
                    prompt = _safe_format(
                        member_cfg.get("question_format", ""),
                        question=next_question,
                        timeout=timeout,
                        max_attempts=max_attempts,
                        remaining=remaining,
                        attempts=attempts,
                        user_name=user_name,
                        user_id=user_id,
                    )
                if not await self._send_group(event, group_id, prompt, at_member, user_id):
                    return
                # 题面已经随失败提示发出，下一轮直接等待作答
                current = (next_question, next_answer)

            async with self.lock:
                record = self.memory.get(key) or {}
                last_answer = str(record.get("last_answer", last_answer))
            await self._handle_member_verification_failed(
                event,
                group_id,
                user_id,
                user_name,
                fail_cfg,
                question=last_question,
                answer=last_answer,
                timed_out=last_timed_out,
            )
        except asyncio.CancelledError:
            logger.info(f"[娜娜群管] 用户 {user_id}（群 {group_id}）的验证任务被取消")
            raise
        except Exception as exc:
            logger.exception(f"[娜娜群管] 用户 {user_id} 的验证流程异常: {exc}")
        finally:
            async with self.lock:
                record = self.memory.get(key)
                if record is not None and not record.get("pending_decision"):
                    self.memory.pop(key, None)
            self.verify_tasks.pop(key, None)
            self.store.save()

    async def _finish_member_verification_ok(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        member_cfg: dict,
    ) -> None:
        """验证通过：发送成功文案。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。
            user_name: 成员昵称。
            member_cfg: 入群后验证配置。
        """
        text = str(member_cfg.get("success_reply", "")).strip()
        if text:
            await self._send_group(
                event,
                group_id,
                _safe_format(text, user_name=user_name, user_id=user_id),
                bool(member_cfg.get("at_member", True)),
                user_id,
            )
        logger.info(f"[娜娜群管] {user_name}({user_id}) 通过群 {group_id} 的入群验证")

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        """把群内消息当作验证答案进行判定。

        Args:
            event: AstrBot 消息事件。
        """
        group_id = event.get_group_id()
        if not group_id:
            return
        user_id = event.get_sender_id()
        key = f"{group_id}:{user_id}"

        async with self.lock:
            record = self.memory.get(key)
            if not record or record.get("kind") != "member_verify":
                return
            future = record.get("future")
            if future is None or future.done():
                return

            answer_text = self._message_plain_text(event).strip()
            record["last_answer"] = answer_text or "（空）"
            if re.fullmatch(r"[+-]?\d+", answer_text):
                outcome = (
                    "correct" if int(answer_text) == int(record.get("answer", 0)) else "wrong"
                )
            else:
                outcome = "non_number"
            record.pop("future", None)
            logger.debug(
                f"[娜娜群管] {user_id}@群{group_id} 作答「{_mask(answer_text, 12)}」→ {outcome}"
            )

        future.set_result(outcome)

    def _message_plain_text(self, event: AstrMessageEvent) -> str:
        """提取消息中的纯文本（去掉 @机器人 等组件）。

        Args:
            event: AstrBot 消息事件。

        Returns:
            拼接后的纯文本；没有文本组件时回退到 ``message_str``。
        """
        parts = [
            str(component.text)
            for component in event.get_messages()
            if isinstance(component, Plain)
        ]
        text = "".join(parts).strip()
        return text or event.message_str.strip()

    # ══════════════════════════════════════════════
    # 三、验证失败处理
    # ══════════════════════════════════════════════

    async def _handle_member_verification_failed(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
        fail_cfg: dict,
        *,
        question: str,
        answer: str,
        timed_out: bool,
    ) -> None:
        """按配置执行禁言 / 踢出 / 二次验证。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。
            user_name: 成员昵称。
            fail_cfg: 失败处理配置。
            question: 最后一题的题面。
            answer: 成员的最后一次回答。
            timed_out: 最后一次失败是否为超时未作答。
        """
        action = str(fail_cfg.get("action", "二次验证"))
        mute_seconds = _to_int(fail_cfg.get("mute_seconds", 600), 600)
        reason = "超时未作答" if timed_out else f"回答错误（{answer}）"

        # 延迟期间成员可能已经退群，先确认还在群里
        if not await self._is_member(event, group_id, user_id):
            logger.info(f"[娜娜群管] {user_id} 已不在群 {group_id}，跳过失败处理")
            return

        # 机器人不是群管理员时禁言/踢出会被平台拒绝，提前告知管理员而不是假装成功
        bot_role = await self._bot_role(event, group_id)
        can_act = bot_role in ("admin", "owner")
        if not can_act:
            logger.warning(
                f"[娜娜群管] 机器人在群 {group_id} 的角色为 {bot_role or '未知'}，"
                f"无法对 {user_id} 执行禁言/踢出，将改为提示管理员手动处理"
            )

        if action == "踢出":
            text = _safe_format(
                fail_cfg.get("kick_reply", ""),
                user_name=user_name,
                user_id=user_id,
                question=question,
                answer=answer,
                reason=reason,
            ).strip()
            if text:
                await self._send_group(event, group_id, text, False, user_id)
            # 留出极短时间让提示先送达，再执行移出
            await asyncio.sleep(0.4)
            ok = await self._kick(
                event, group_id, user_id, bool(fail_cfg.get("kick_blacklist", False))
            )
            logger.info(
                f"[娜娜群管] {user_name}({user_id}) 验证失败已踢出群 {group_id}"
                f"（成功={ok}，机器人角色={bot_role or '未知'}）"
            )
            if not ok:
                await self._send_group(
                    event,
                    group_id,
                    f"⚠️ 机器人无权限移出 {user_name}（{user_id}），请管理员手动处理。",
                    False,
                    user_id,
                )
            return

        if action == "禁言":
            muted = await self._mute(event, group_id, user_id, mute_seconds)
            text = _safe_format(
                fail_cfg.get("mute_reply", ""),
                user_name=user_name,
                user_id=user_id,
                mute_seconds=mute_seconds,
                question=question,
                answer=answer,
                reason=reason,
            ).strip()
            if muted:
                if text:
                    await self._send_group(event, group_id, text, False, user_id)
            else:
                await self._send_group(
                    event,
                    group_id,
                    f"⚠️ 机器人无权限禁言 {user_name}（{user_id}），请管理员手动处理。",
                    False,
                    user_id,
                )
            logger.info(
                f"[娜娜群管] {user_name}({user_id}) 验证失败已在群 {group_id} 禁言 "
                f"{mute_seconds}s（成功={muted}，机器人角色={bot_role or '未知'}）"
            )
            return

        # 二次验证：群内禁言 + 到通知群提醒管理员
        muted = await self._mute(event, group_id, user_id, mute_seconds)
        text = _safe_format(
            fail_cfg.get("secondary_group_reply", ""),
            user_name=user_name,
            user_id=user_id,
            mute_seconds=mute_seconds,
            reason=reason,
        ).strip()
        if muted:
            if text:
                await self._send_group(event, group_id, text, False, user_id)
        else:
            # 不能宣称"已禁言"，只说明已提交管理员处理
            await self._send_group(
                event,
                group_id,
                _safe_format(
                    "⚠️ {user_name}（{user_id}）未通过入群验证，"
                    "机器人无权限禁言，已提交管理员处理。",
                    user_name=user_name,
                    user_id=user_id,
                ),
                False,
                user_id,
            )

        key = f"{group_id}:{user_id}"
        async with self.lock:
            # 覆盖记录前先取出验证阶段写入的尝试次数，供通知模板使用
            max_attempts = self.memory.get(key, {}).get("max_attempts", "")
            self._prune_memory()
            self.memory[key] = {
                "kind": "secondary_pending",
                "pending_decision": True,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "question": question,
                "answer": answer,
                "reason": reason,
                "mute_seconds": mute_seconds,
                "muted": muted,
                "max_attempts": max_attempts,
                "timed_out": timed_out,
                "created_at": time.time(),
            }
        self.store.save()

        if fail_cfg.get("secondary_notify_errors_only", False) and timed_out:
            logger.info(
                f"[娜娜群管] {user_id} 因超时失败且配置为仅答错时通知，跳过通知群提醒"
            )
        else:
            # 先起超时兜底任务，通知发送期间超时也能正常触发
            await self._start_decision_timer(
                event, key, group_id, user_id, user_name, fail_cfg
            )
            await self._notify_secondary(event, group_id, user_id, user_name)
            return

        await self._start_decision_timer(
            event, key, group_id, user_id, user_name, fail_cfg
        )

    def _action_note(
        self,
        fail_cfg: dict,
        *,
        muted: bool,
        bot_role: str | None,
        kicked: bool | None = None,
    ) -> str:
        """生成"本群实际执行了什么"的说明，供通知群与指令回复使用。

        机器人不是管理员时禁言/踢出会被平台拒绝，这里如实说明，
        避免管理员误以为动作已经生效。

        Args:
            fail_cfg: 失败处理配置。
            muted: 禁言是否执行成功。
            bot_role: 机器人在原群的角色，``None`` 表示查询失败。
            kicked: 踢出是否执行成功，非踢出流程时为 ``None``。

        Returns:
            面向管理员的单行说明文本。
        """
        if bot_role is None:
            return "⚠️ 无法确认机器人权限，请人工核对本群处理结果"
        if bot_role not in ("admin", "owner"):
            return "❌ 机器人非本群管理员，禁言/踢出未执行，请手动处理"
        if kicked is False:
            return "❌ 踢出失败，请检查机器人权限后手动处理"
        if kicked:
            return "✅ 已移出群聊"
        if muted:
            seconds = _to_int(fail_cfg.get("mute_seconds", 600), 600)
            return f"✅ 已禁言 {seconds} 秒"
        return "❌ 禁言失败，请检查机器人权限后手动处理"

    async def _notify_secondary(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        user_name: str,
    ) -> None:
        """把二次验证提醒发送到通知群，失败时退化为私聊群主/管理员。

        Args:
            event: AstrBot 消息事件。
            group_id: 原群号。
            user_id: 待处理成员 QQ 号。
            user_name: 待处理成员昵称。
        """
        settings, _, _, fail_cfg, notify_group = self._group_config(group_id)
        async with self.lock:
            record = dict(self.memory.get(f"{group_id}:{user_id}") or {})
        bot_role = await self._bot_role(event, group_id)
        muted = bool(record.get("muted", False))
        text = _safe_format(
            settings.get("notify_group_template", ""),
            user_id=user_id,
            user_name=user_name,
            group_id=group_id,
            question=record.get("question", ""),
            answer=record.get("answer", ""),
            reason=record.get("reason", ""),
            attempts=record.get("max_attempts", ""),
            mute_seconds=record.get("mute_seconds", ""),
            time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            bot_role=bot_role or "未知",
            action_note=self._action_note(fail_cfg, muted=muted, bot_role=bot_role),
        ).strip()
        if not text:
            return

        if notify_group.isdigit():
            # 通知群通常由同一个机器人实例服务，沿用当前事件发送即可
            if await self._send_group(event, notify_group, text, False, None):
                logger.info(
                    f"[娜娜群管] 已向通知群 {notify_group} 发送二次验证提醒：{user_id}"
                )
                return
            logger.warning(
                f"[娜娜群管] 通知群 {notify_group} 发送失败，尝试私聊群主/管理员"
            )

        if not settings.get("notify_owner_admin", True):
            return
        owner, admins = await self._group_owner_and_admins(event, group_id)
        targets = [target for target in [owner, *admins] if target]
        if not targets:
            logger.warning(f"[娜娜群管] 群 {group_id} 没有可通知的群主/管理员")
            return
        for target in targets:
            await self._send_private(event, target, text)

    async def _start_decision_timer(
        self,
        event: AstrMessageEvent,
        key: str,
        group_id: str,
        user_id: str,
        user_name: str,
        fail_cfg: dict,
    ) -> None:
        """启动管理员处理超时计时器。

        Args:
            event: AstrBot 消息事件。
            key: ``群号:QQ号`` 形式的验证键。
            group_id: 原群号。
            user_id: 待处理成员 QQ 号。
            user_name: 待处理成员昵称。
            fail_cfg: 失败处理配置。
        """
        timeout = _to_int(fail_cfg.get("admin_decision_timeout", 300), 300)
        if timeout <= 0:
            return
        old = self.decision_tasks.pop(key, None)
        if old and not old.done():
            old.cancel()

        async def _watch() -> None:
            """等待管理员决策，超时后按配置执行兜底处理。"""
            try:
                await asyncio.sleep(timeout)
            except asyncio.CancelledError:
                return
            async with self.lock:
                record = self.memory.get(key)
                if not record or record.get("kind") != "secondary_pending":
                    return
                self.memory.pop(key, None)
                mute_seconds = _to_int(record.get("mute_seconds", 600), 600)
                was_muted = bool(record.get("muted", False))
            self.store.save()

            action = str(fail_cfg.get("decision_timeout_action", "保持禁言"))
            bot_role = await self._bot_role(event, group_id)
            can_act = bot_role in ("admin", "owner")
            if action == "踢出":
                ok = await self._kick(event, group_id, user_id, False)
                await self._send_group(
                    event,
                    group_id,
                    _safe_format(
                        ("管理员未在 {timeout} 秒内处理，{user_name} 已被移出本群。")
                        if ok
                        else (
                            "管理员未在 {timeout} 秒内处理，且机器人无权限移出 "
                            "{user_name}，请手动处理。"
                        ),
                        timeout=timeout,
                        user_name=user_name,
                        user_id=user_id,
                    ),
                    False,
                    user_id,
                )
            elif action == "解除禁言":
                await self._unmute(event, group_id, user_id)
            elif not was_muted and can_act:
                # 之前禁言没生效时兜底重试一次，避免"只通知不处理"
                await self._mute(event, group_id, user_id, mute_seconds)
            elif not was_muted:
                # 机器人无管理权限，禁言补救不可能成功，如实提示管理员
                await self._send_group(
                    event,
                    group_id,
                    f"管理员未在 {timeout} 秒内处理 {user_name}，"
                    "且机器人非本群管理员，无法禁言，请手动处理。",
                    False,
                    user_id,
                )
            logger.info(
                f"[娜娜群管] {user_id} 二次验证超时（{timeout}s），已执行兜底处理：{action}"
                f"（机器人角色={bot_role or '未知'}）"
            )

        task = asyncio.create_task(_watch())
        task.set_name(f"nana_decision_{group_id}_{user_id}")
        self.decision_tasks[key] = task
        task.add_done_callback(
            lambda t, k=key: self._on_task_done(t, self.decision_tasks, k)
        )

    # ══════════════════════════════════════════════
    # 管理员指令
    # ══════════════════════════════════════════════

    async def _is_operator(self, event: AstrMessageEvent) -> bool:
        """判断指令发送者是否有权处理验证。

        允许三类人：当前群的群主/管理员、通知群内的群主/管理员、AstrBot 配置的管理员。

        Args:
            event: AstrBot 消息事件。

        Returns:
            是否有处理权限。
        """
        if event.is_admin():
            return True
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id:
            return False
        owner, admins = await self._group_owner_and_admins(event, group_id)
        if sender_id == owner or sender_id in admins:
            return True
        settings = self.config.get("global", {})
        notify_group = (
            str(settings.get("notify_group", "")).strip()
            if isinstance(settings, dict)
            else ""
        )
        # 通知群的群主/管理员可以代其他群处理待办
        return bool(notify_group) and group_id == notify_group

    def _parse_target(self, event: AstrMessageEvent, command: str) -> str | None:
        """从指令中解析目标 QQ 号，支持 @成员 或直接输入数字。

        Args:
            event: AstrBot 消息事件。
            command: 指令名，用于剥离参数前缀。

        Returns:
            目标 QQ 号；解析失败返回 ``None``。
        """
        for component in event.get_messages():
            if isinstance(component, At):
                return str(component.qq)
        text = event.message_str.strip()
        remainder = text[len(command) :] if text.startswith(command) else text
        digits = re.findall(r"\d{4,}", remainder)
        return digits[0] if digits else None

    def _find_pending(self, target_id: str, current_group: str) -> list[str]:
        """查找与该 QQ 号相关的待处理二次验证记录键。

        Args:
            target_id: 目标 QQ 号。
            current_group: 指令所在群号，优先匹配该群的记录。

        Returns:
            匹配到的记录键列表。
        """
        keys = [
            key
            for key, record in self.memory.items()
            if record.get("kind") == "secondary_pending"
            and str(record.get("user_id")) == target_id
        ]
        preferred = [key for key in keys if key.startswith(f"{current_group}:")]
        return preferred or keys

    @filter.command("通过验证")
    async def cmd_approve(self, event: AstrMessageEvent):
        """解除二次验证成员的禁言。用法：/通过验证 <@成员或QQ号>"""
        if not await self._is_operator(event):
            return
        target_id = self._parse_target(event, "/通过验证")
        if not target_id:
            yield event.plain_result(
                "请指定成员，例如：/通过验证 @成员 或 /通过验证 123456789"
            )
            return
        keys = self._find_pending(target_id, event.get_group_id() or "")
        if not keys:
            yield event.plain_result("没有找到该成员待处理的验证记录")
            return

        handled: list[str] = []
        failed: list[str] = []
        for key in keys:
            async with self.lock:
                record = self.memory.pop(key, None)
            if not record:
                continue
            task = self.decision_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()
            ok = await self._unmute(event, record["group_id"], record["user_id"])
            role = await self._bot_role(event, record["group_id"])
            if ok:
                await self._send_group(
                    event,
                    record["group_id"],
                    f"{record.get('user_name', record['user_id'])} 已通过管理员二次验证，禁言已解除。",
                    False,
                    record["user_id"],
                )
                handled.append(str(record["group_id"]))
            else:
                failed.append(str(record["group_id"]))
                await self._send_group(
                    event,
                    record["group_id"],
                    f"⚠️ 管理员已通过 {record.get('user_name', record['user_id'])}，"
                    f"但机器人（角色：{role or '未知'}）解除禁言失败，请手动处理。",
                    False,
                    record["user_id"],
                )
            logger.info(
                f"[娜娜群管] 管理员 {event.get_sender_id()} 通过验证 {target_id}"
                f"（群 {record['group_id']}，解除禁言成功={ok}）"
            )
        self.store.save()
        parts = []
        if handled:
            parts.append(f"已通过 {target_id} 的二次验证并解除禁言（群：{'、'.join(handled)}）")
        if failed:
            parts.append(
                f"⚠️ {target_id} 已标记通过，但以下群解除禁言失败，请检查机器人权限："
                f"{'、'.join(failed)}"
            )
        yield event.plain_result("\n".join(parts) if parts else "处理失败，记录已失效")

    @filter.command("拒绝验证")
    async def cmd_reject(self, event: AstrMessageEvent):
        """拒绝二次验证成员并移出群聊。用法：/拒绝验证 <@成员或QQ号>"""
        if not await self._is_operator(event):
            return
        target_id = self._parse_target(event, "/拒绝验证")
        if not target_id:
            yield event.plain_result(
                "请指定成员，例如：/拒绝验证 @成员 或 /拒绝验证 123456789"
            )
            return
        keys = self._find_pending(target_id, event.get_group_id() or "")
        if not keys:
            yield event.plain_result("没有找到该成员待处理的验证记录")
            return

        handled: list[str] = []
        failed: list[str] = []
        for key in keys:
            async with self.lock:
                record = self.memory.pop(key, None)
            if not record:
                continue
            task = self.decision_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()
            ok = await self._kick(event, record["group_id"], record["user_id"], True)
            role = await self._bot_role(event, record["group_id"])
            if ok:
                await self._send_group(
                    event,
                    record["group_id"],
                    f"{record.get('user_name', record['user_id'])} 未通过二次验证，已被移出本群。",
                    False,
                    record["user_id"],
                )
                handled.append(str(record["group_id"]))
            else:
                failed.append(str(record["group_id"]))
                await self._send_group(
                    event,
                    record["group_id"],
                    f"⚠️ 机器人（角色：{role or '未知'}）无权限移出 "
                    f"{record.get('user_name', record['user_id'])}，请手动处理。",
                    False,
                    record["user_id"],
                )
            logger.info(
                f"[娜娜群管] 管理员 {event.get_sender_id()} 拒绝验证 {target_id}"
                f"（群 {record['group_id']}，踢出成功={ok}）"
            )
        self.store.save()
        parts = []
        if handled:
            parts.append(f"已拒绝 {target_id} 并移出群聊（群：{'、'.join(handled)}）")
        if failed:
            parts.append(
                f"⚠️ {target_id} 已标记拒绝，但以下群移出失败，请检查机器人权限："
                f"{'、'.join(failed)}"
            )
        yield event.plain_result("\n".join(parts) if parts else "处理失败，记录已失效")

    @filter.command("解除禁言")
    async def cmd_unmute(self, event: AstrMessageEvent):
        """为指定成员解除禁言。用法：/解除禁言 <@成员或QQ号>"""
        if not await self._is_operator(event):
            return
        group_id = event.get_group_id()
        target_id = self._parse_target(event, "/解除禁言")
        if not group_id or not target_id:
            yield event.plain_result(
                "请在群内使用：/解除禁言 @成员 或 /解除禁言 123456789"
            )
            return
        ok = await self._unmute(event, group_id, target_id)
        yield event.plain_result(
            f"已解除 {target_id} 的禁言"
            if ok
            else f"解除 {target_id} 禁言失败，请检查机器人是否有管理员权限"
        )

    @filter.command("群管状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前群的验证配置与待处理记录。"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("该指令仅在群聊中可用")
            return
        settings, join_cfg, member_cfg, fail_cfg, notify_group = self._group_config(
            group_id
        )
        bot_role = await self._bot_role(event, group_id)
        can_act = bot_role in ("admin", "owner")
        async with self.lock:
            pending = [
                record
                for record in self.memory.values()
                if record.get("kind") == "secondary_pending"
                and str(record.get("group_id")) == group_id
            ]
        lines = [
            f"【娜娜群管】群 {group_id} 当前配置",
            f"总开关：{'开启' if settings.get('enabled', True) else '关闭'}",
            f"机器人权限：{bot_role or '未知'}"
            + ("" if can_act else "（禁言/踢出/审批将无法执行，请把机器人设为管理员！）"),
            f"入群申请验证：{'开启' if join_cfg.get('enabled', True) else '关闭'}"
            f" | 正则：{join_cfg.get('answer_pattern', '')}"
            f" | AI 判定：{'开启' if join_cfg.get('use_llm', False) else '关闭'}"
            f"（{join_cfg.get('llm_mode', '')}）",
            f"入群后验证：{'开启' if member_cfg.get('enabled', True) else '关闭'}"
            f" | 难度：{member_cfg.get('difficulty', '')}"
            f" | 时限：{member_cfg.get('verify_timeout', 0)}s"
            f" | 次数：{member_cfg.get('max_attempts', 0)}",
            f"失败处理：{fail_cfg.get('action', '')}"
            f" | 禁言：{fail_cfg.get('mute_seconds', 0)}s"
            f" | 通知群：{notify_group or '（未配置，改为私聊管理员）'}",
            f"待处理二次验证：{len(pending)} 条",
        ]
        for record in pending[:10]:
            lines.append(
                f"  · {record.get('user_name')}（{record.get('user_id')}）"
                f" {record.get('reason', '')}"
            )
        yield event.plain_result("\n".join(lines))

    # ══════════════════════════════════════════════
    # OneBot 能力封装
    # ══════════════════════════════════════════════

    async def _send_group(
        self,
        event: AstrMessageEvent,
        group_id: str,
        text: str,
        at_member: bool,
        user_id: str | None,
    ) -> bool:
        """向群聊发送文本，可选 @成员。

        Args:
            event: AstrBot 消息事件，用于取到机器人实例。
            group_id: 目标群号。
            text: 文本内容。
            at_member: 是否在文本前 @ 指定成员。
            user_id: 被 @ 的成员 QQ 号。

        Returns:
            是否发送成功。
        """
        if not str(text).strip():
            return False
        try:
            if at_member and user_id:
                await event.bot.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=[
                        {"type": "at", "data": {"qq": str(user_id)}},
                        {"type": "text", "data": {"text": " " + text}},
                    ],
                )
            else:
                await event.bot.api.call_action(
                    "send_group_msg",
                    group_id=int(group_id),
                    message=text,
                )
            return True
        except Exception as exc:
            logger.warning(f"[娜娜群管] 向群 {group_id} 发送消息失败: {exc}")
            return False

    async def _send_private(
        self, event: AstrMessageEvent, user_id: str, text: str
    ) -> bool:
        """发送私聊消息，先尝试群临时会话再退化为普通私聊。

        Args:
            event: AstrBot 消息事件。
            user_id: 目标 QQ 号。
            text: 文本内容。

        Returns:
            是否发送成功。
        """
        group_id = event.get_group_id()
        if group_id and str(group_id).isdigit():
            try:
                await event.bot.api.call_action(
                    "send_private_msg",
                    user_id=int(user_id),
                    group_id=int(group_id),
                    message=text,
                )
                return True
            except Exception:
                pass
        try:
            await event.bot.api.call_action(
                "send_private_msg", user_id=int(user_id), message=text
            )
            return True
        except Exception as exc:
            logger.warning(f"[娜娜群管] 向 {user_id} 发送私聊失败: {exc}")
            return False

    async def _mute(
        self, event: AstrMessageEvent, group_id: str, user_id: str, seconds: int
    ) -> bool:
        """禁言成员。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。
            seconds: 禁言时长，0 表示解除禁言。

        Returns:
            是否禁言成功。
        """
        try:
            await event.bot.api.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=max(0, seconds),
            )
            return True
        except Exception as exc:
            logger.error(f"[娜娜群管] 禁言 {user_id} 失败: {exc}")
            return False

    async def _unmute(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> bool:
        """解除成员禁言。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。

        Returns:
            是否解除成功。
        """
        try:
            await event.bot.api.call_action(
                "set_group_ban",
                group_id=int(group_id),
                user_id=int(user_id),
                duration=0,
            )
            return True
        except Exception as exc:
            logger.error(f"[娜娜群管] 解除 {user_id} 禁言失败: {exc}")
            return False

    async def _kick(
        self,
        event: AstrMessageEvent,
        group_id: str,
        user_id: str,
        blacklist: bool,
    ) -> bool:
        """把成员移出群聊。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。
            blacklist: 是否同时拒绝其再次加群。

        Returns:
            是否踢出成功。
        """
        try:
            await event.bot.api.call_action(
                "set_group_kick",
                group_id=int(group_id),
                user_id=int(user_id),
                reject_add_request=bool(blacklist),
            )
            return True
        except Exception as exc:
            logger.error(f"[娜娜群管] 踢出 {user_id} 失败: {exc}")
            return False

    async def _is_member(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> bool:
        """检查成员是否仍在群内。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。

        Returns:
            成员在群内返回 ``True``；接口异常时保守返回 ``True``。
        """
        try:
            info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            return bool(info)
        except Exception:
            return True

    async def _bot_role(self, event: AstrMessageEvent, group_id: str) -> str | None:
        """查询机器人在指定群的角色，用于判断能否执行禁言/踢出。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。

        Returns:
            ``owner`` / ``admin`` / ``member``；查询失败时返回 ``None``。
        """
        self_id = str(event.get_self_id())
        try:
            info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(self_id),
                no_cache=True,
            )
        except Exception as exc:
            logger.warning(f"[娜娜群管] 查询机器人在群 {group_id} 的角色失败: {exc}")
            return None
        if isinstance(info, dict) and info.get("role"):
            return str(info["role"])
        return "member"

    async def _group_owner_and_admins(
        self, event: AstrMessageEvent, group_id: str
    ) -> tuple[str | None, list[str]]:
        """获取群主与管理员列表。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。

        Returns:
            ``(群主 QQ 号或 None, 管理员 QQ 号列表)``。
        """
        try:
            members = await event.bot.api.call_action(
                "get_group_member_list", group_id=int(group_id)
            )
        except Exception as exc:
            logger.warning(f"[娜娜群管] 获取群 {group_id} 成员列表失败: {exc}")
            return None, []
        if not isinstance(members, list):
            return None, []
        owner: str | None = None
        admins: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            uid = str(member.get("user_id", ""))
            role = member.get("role")
            if role == "owner":
                owner = uid
            elif role == "admin":
                admins.append(uid)
        return owner, admins

    async def _display_name(
        self, event: AstrMessageEvent, group_id: str, user_id: str
    ) -> str:
        """获取成员群名片或昵称。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。
            user_id: 成员 QQ 号。

        Returns:
            展示名称；获取失败时返回 QQ 号。
        """
        try:
            info = await event.bot.api.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            if isinstance(info, dict):
                return str(info.get("card") or info.get("nickname") or user_id)
        except Exception as exc:
            logger.debug(f"[娜娜群管] 获取 {user_id} 群名片失败: {exc}")
        return user_id

    async def _group_name(self, event: AstrMessageEvent, group_id: str) -> str:
        """获取群名称。

        Args:
            event: AstrBot 消息事件。
            group_id: 群号。

        Returns:
            群名称；获取失败时返回群号。
        """
        try:
            info = await event.bot.api.call_action(
                "get_group_info", group_id=int(group_id)
            )
            if isinstance(info, dict) and info.get("group_name"):
                return str(info["group_name"])
        except Exception:
            pass
        return group_id

    async def _applicant_profile(
        self, event: AstrMessageEvent, user_id: str, raw: Any
    ) -> tuple[str, Any]:
        """获取申请人的昵称与 QQ 等级，用于 AI 判定与低等级拦截。

        Args:
            event: AstrBot 消息事件。
            user_id: 申请人 QQ 号。
            raw: OneBot 原始 request 事件。

        Returns:
            ``(昵称, 等级)``；等级未知时返回 ``"未知"``。
        """
        level: Any = _raw_field(raw, "level", "")
        name = str(_raw_field(raw, "nickname", "") or "")
        try:
            info = await event.bot.api.call_action(
                "get_stranger_info", user_id=int(user_id), no_cache=True
            )
            if isinstance(info, dict):
                name = str(info.get("nickname") or name)
                for field in ("level", "qq_level"):
                    if level in ("", None) and info.get(field) not in (None, ""):
                        level = info[field]
        except Exception as exc:
            logger.debug(f"[娜娜群管] 获取申请人 {user_id} 资料失败: {exc}")

        if not name:
            name = user_id
        if level in ("", None):
            level = "未知"
        else:
            try:
                level = int(level)
            except (TypeError, ValueError):
                level = str(level)
        return name, level

    # ══════════════════════════════════════════════
    # 工具
    # ══════════════════════════════════════════════

    def _make_question(self, member_cfg: dict) -> tuple[str, int]:
        """生成加减法题目，答案保证为整数。

        「简单（10以内加减）」在关闭负数后保证两加数与结果都落在 0-10 之间，
        符合低门槛的群管验证习惯。

        Args:
            member_cfg: 入群后验证配置。

        Returns:
            ``(题面, 答案)``。
        """
        difficulty = str(member_cfg.get("difficulty", "简单（10以内加减）"))
        simple = "简单" in difficulty
        low, high = (1, 9) if simple else (10, 99)
        allow_negative = bool(member_cfg.get("allow_subtract_negative", False))

        if random.random() < 0.5:
            if simple and not allow_negative:
                a = random.randint(0, 10)
                b = random.randint(0, 10 - a)
            else:
                a = random.randint(low, high)
                b = random.randint(low, high)
            return f"{a} + {b}", a + b

        a = random.randint(low, high)
        b = random.randint(low, high)
        if not allow_negative and b > a:
            a, b = b, a
        return f"{a} - {b}", a - b

    def _prune_memory(self) -> None:
        """限制内存中记录数量，优先丢弃最旧的验证记录并保留待决策记录。"""
        if len(self.memory) <= MAX_PENDING_RECORDS:
            return
        ordered = sorted(
            self.memory.items(), key=lambda item: item[1].get("created_at", 0)
        )
        overflow = len(self.memory) - MAX_PENDING_RECORDS
        for key, record in ordered:
            if overflow <= 0:
                break
            if record.get("kind") == "secondary_pending":
                continue
            future = record.get("future")
            if future is not None and not future.done():
                future.cancel()
            self.memory.pop(key, None)
            overflow -= 1

    def _on_task_done(
        self, task: asyncio.Task, registry: dict[str, asyncio.Task], key: str
    ) -> None:
        """任务结束回调：清理注册表并记录未处理异常。

        Args:
            task: 结束的任务。
            registry: 任务注册表。
            key: 注册键。
        """
        registry.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"[娜娜群管] 后台任务 {key} 异常: {exc}")
