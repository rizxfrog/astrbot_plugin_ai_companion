"""配置读取门面。

AstrBot 会把 ``_conf_schema.json`` 解析为默认配置并合并用户配置后，通过
``Star.__init__(context, config)`` 注入。这里只做防御性读取与类型归一，
不重复实现 schema 逻辑。
"""

from __future__ import annotations

from typing import Any


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_str_list(value: Any) -> list[str]:
    """归一为字符串列表（兼容平台配置里可能出现的字符串形式）。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


class CompanionConfig:
    """插件配置的强类型视图。"""

    def __init__(self, raw: dict | None) -> None:
        self.raw: dict = dict(raw or {})
        self.enable = _as_bool(self.raw.get("enable"), True)
        self.enable_private_chat = _as_bool(self.raw.get("enable_private_chat"), True)
        self.enable_group_chat = _as_bool(self.raw.get("enable_group_chat"), True)
        self.reply_probability = max(
            0.0, min(1.0, _as_float(self.raw.get("reply_probability"), 0.85))
        )
        # -1 表示「沿用 reply_probability」
        _group_p = _as_float(self.raw.get("group_reply_probability"), -1.0)
        self.group_reply_probability = (
            -1.0 if _group_p < 0 else max(0.0, min(1.0, _group_p))
        )
        self.enable_llm_judge = _as_bool(self.raw.get("enable_llm_judge"), True)
        self.judge_provider_id = _as_str(self.raw.get("judge_provider_id"), "")
        self.judge_timeout_seconds = max(
            1, _as_int(self.raw.get("judge_timeout_seconds"), 15)
        )
        self.ignore_command_messages = _as_bool(
            self.raw.get("ignore_command_messages"), True
        )
        self.min_reply_interval_seconds = max(
            0, _as_int(self.raw.get("min_reply_interval_seconds"), 3)
        )
        self.cooldown_after_unanswered = max(
            0, _as_int(self.raw.get("cooldown_after_unanswered"), 0)
        )
        self.record_all_messages = _as_bool(self.raw.get("record_all_messages"), True)
        self.enable_compact = _as_bool(self.raw.get("enable_compact"), True)
        self.compact_trigger_turns = max(
            4, _as_int(self.raw.get("compact_trigger_turns"), 60)
        )
        self.compact_keep_recent = max(
            0, _as_int(self.raw.get("compact_keep_recent"), 20)
        )
        self.compact_min_dropped = max(
            1, _as_int(self.raw.get("compact_min_dropped"), 10)
        )
        self.compact_provider_id = _as_str(self.raw.get("compact_provider_id"), "")
        # --- 主动消息 ---
        self.enable_proactive = _as_bool(self.raw.get("enable_proactive"), False)
        self.proactive_sessions = _as_str_list(self.raw.get("proactive_sessions"))
        self.proactive_group = _as_bool(self.raw.get("proactive_group"), True)
        self.proactive_private = _as_bool(self.raw.get("proactive_private"), True)
        self.proactive_threshold_minutes = max(
            1, _as_int(self.raw.get("proactive_threshold_minutes"), 60)
        )
        self.proactive_check_interval_seconds = max(
            5, _as_int(self.raw.get("proactive_check_interval_seconds"), 60)
        )
        self.proactive_max_unanswered = max(
            0, _as_int(self.raw.get("proactive_max_unanswered"), 2)
        )
        self.proactive_cooldown_minutes = max(
            0, _as_int(self.raw.get("proactive_cooldown_minutes"), 240)
        )
        self.proactive_history_turns = max(
            0, _as_int(self.raw.get("proactive_history_turns"), 10)
        )
        self.proactive_provider_id = _as_str(
            self.raw.get("proactive_provider_id"), ""
        )
        self.proactive_prompt = _as_str(self.raw.get("proactive_prompt"), "")
        # --- 人物与关系 ---
        self.enable_knowledge_extraction = _as_bool(
            self.raw.get("enable_knowledge_extraction"), True
        )
        self.extraction_provider_id = _as_str(
            self.raw.get("extraction_provider_id"), ""
        )
        self.extraction_interval_minutes = max(
            1, _as_int(self.raw.get("extraction_interval_minutes"), 30)
        )
        self.extraction_min_messages = max(
            1, _as_int(self.raw.get("extraction_min_messages"), 8)
        )
        self.extraction_batch_size = max(
            5, _as_int(self.raw.get("extraction_batch_size"), 40)
        )
        self.inject_people_context = _as_bool(
            self.raw.get("inject_people_context"), True
        )
        self.inject_events_context = _as_bool(
            self.raw.get("inject_events_context"), True
        )
        # --- 拟人增强 ---
        self.enable_stickers = _as_bool(self.raw.get("enable_stickers"), True)
        self.sticker_auto_probability = max(
            0.0, min(1.0, _as_float(self.raw.get("sticker_auto_probability"), 0.15))
        )
        self.enable_typos = _as_bool(self.raw.get("enable_typos"), False)
        self.typo_probability = max(
            0.0, min(1.0, _as_float(self.raw.get("typo_probability"), 0.03))
        )
        self.system_prompt_extra = _as_str(self.raw.get("system_prompt_extra"), "")
        self.inject_time = _as_bool(self.raw.get("inject_time"), True)
        self.debug_mode = _as_bool(self.raw.get("debug_mode"), False)

    def enabled_for(self, *, is_private: bool) -> bool:
        if not self.enable:
            return False
        return self.enable_private_chat if is_private else self.enable_group_chat

    def refresh(self, raw: dict | None) -> None:
        """配置热更新时重建视图。"""
        self.__init__(raw)
