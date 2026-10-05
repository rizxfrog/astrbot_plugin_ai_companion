"""把平台消息链渲染为可读文本。

设计目标：**只做无损的事实陈述**，不做价值判断。渲染结果既用于落库（供检索
与长期记忆），也用于拼装给模型的上下文。系统级信息（时间/发送者）以结构化
前缀呈现，与用户正文严格分离，避免用户通过消息内容伪造身份。
"""

from __future__ import annotations

from typing import Any

# 组件类型 -> 占位符。图片等富媒体不直接把路径塞进历史文本（既贵又无意义），
# 具体内容交由平台的多模态/图片理解链路处理。
_PLACEHOLDERS = {
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "nodes": "[合并转发]",
    "music": "[音乐]",
    "face": "[表情]",
    "json": "[卡片]",
    "share": "[分享]",
    "location": "[位置]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "shake": "[窗口抖动]",
}


def render_chain(components: list[Any] | None, *, self_id: str = "") -> str:
    """把消息链渲染为一行可读文本。"""
    if not components:
        return ""

    parts: list[str] = []
    for comp in components:
        ctype = _normalize_type(comp)
        value = getattr(comp, "value", ctype)

        if ctype in ("plain", "text"):
            text = getattr(comp, "text", "") or ""
            if text:
                parts.append(text)
        elif ctype == "at":
            qq = str(getattr(comp, "qq", "") or "")
            if qq == "all":
                parts.append("[@全体成员]")
            elif self_id and qq == str(self_id):
                parts.append("[@我]")
            else:
                name = getattr(comp, "name", "") or ""
                parts.append(f"[@{name or qq}]")
        elif ctype == "reply":
            parts.append(_render_reply(comp))
        elif ctype == "face":
            fid = getattr(comp, "id", "")
            parts.append(f"[表情:{fid}]" if fid != "" else "[表情]")
        else:
            placeholder = _PLACEHOLDERS.get(ctype)
            if placeholder is None:
                placeholder = _PLACEHOLDERS.get(str(value))
            parts.append(placeholder or f"[{ctype}]")

    return "".join(parts).strip()


def _normalize_type(comp: Any) -> str:
    """取出组件类型的字符串值。

    ``ComponentType`` 是 ``(str, Enum)``，直接 ``str()`` 会得到
    ``"ComponentType.Plain"``；必须取 ``.value`` 才能拿到 ``"Plain"``。
    """
    ctype = getattr(comp, "type", "") or ""
    ctype = getattr(ctype, "value", ctype)
    return str(ctype).lower()


def _render_reply(comp: Any) -> str:
    """渲染引用消息：``[引用 昵称: 内容]``。"""
    nickname = getattr(comp, "sender_nickname", "") or ""
    content = getattr(comp, "message_str", "") or ""
    if not content:
        chain = getattr(comp, "chain", None)
        if chain:
            content = render_chain(chain)
    if nickname and content:
        return f"[引用 {nickname}: {content}]"
    if content:
        return f"[引用: {content}]"
    return "[引用消息]"


def format_for_model(
    *,
    sender_name: str,
    sender_id: str,
    content: str,
    is_bot: bool = False,
) -> str:
    """把一条消息格式化为注入模型的单行文本。

    发送者信息放在冒号前作为元数据区，正文放在冒号后——用户无法通过正文
    伪造「谁说了什么」。
    """
    label = sender_name or "未知用户"
    if is_bot:
        label = f"{label}(你)"
    sid = f"({sender_id})" if sender_id and sender_name != sender_id else ""
    return f"{label}{sid}: {content}"
