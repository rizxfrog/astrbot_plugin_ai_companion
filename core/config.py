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
