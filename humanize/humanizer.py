"""拟人化处理：装饰即将发送的回复。

在 ``on_decorating_result`` 阶段对消息链做两件事：

1. **表情包**：把 AI 写的 ``[sticker:分类]`` 标记替换成真实的图片组件；
   按概率在结尾追加一张（可选）。
2. **错别字**：按概率制造轻微手误（可选，默认关闭以免影响观感）。

**不在这里做分段与打字延迟**——平台自带的「分段回复」会在本钩子之后立即执行，
并且由 ``RespondStage`` 按对数间隔逐段发送（``respond/stage.py``），
重复实现只会造成双重延迟。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from .stickers import StickerLibrary
from .typo import apply_typos

try:
    from astrbot.core.message.components import Image, Plain
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    Plain = None  # type: ignore[assignment]

# [sticker:开心] / [表情:开心] / [sticker]
STICKER_PATTERN = re.compile(r"\[(?:sticker|表情)(?::([^\]]*))?\]", re.IGNORECASE)

# 模型想发表情/图片但没能真正附上媒体时，会留下这种悬空占位符。
# 直接发出去会让用户看到 "[图片]" 这种字面量，因此在没有实际图片时清掉。
DANGLING_PLACEHOLDER_PATTERN = re.compile(r"\[(?:图片|image|照片|photo)\]", re.IGNORECASE)

# 有些模型会把**工具调用当成文本写进正文**，形如：
#     <invoke name="send_sticker"><parameter name="category">无语</parameter></invoke>
# （Anthropic 风格，可能带 antml: 前缀）。平台的 provider 只认结构化的 tool_calls
# 字段，不会解析这种文本，于是整段 XML 原样发给用户。这里无条件清掉 ——
# 它永远不是想给用户看的内容。
TOOL_CALL_XML_PATTERN = re.compile(
    r"<(?:antml:)?invoke\b[^>]*>.*?</(?:antml:)?invoke>"
    r"|<(?:antml:)?(?:function_calls|function_call)\b[^>]*>.*?</(?:antml:)?(?:function_calls|function_call)>"
    r"|<(?:antml:)?invoke\b[^>]*/?>"
    r"|</?(?:antml:)?(?:invoke|parameter|function_calls|function_call)\b[^>]*>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass
class HumanizeResult:
    replaced: int = 0
    appended: bool = False
    typo: bool = False
    blocked: int = 0


class Humanizer:
    """把回复链改造成更像真人发出的样子。"""

    def __init__(
        self,
        *,
        stickers: StickerLibrary,
        config: Any,
        rng: random.Random | None = None,
    ) -> None:
        self.stickers = stickers
        self.config = config
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _pick_sticker(self, category: str | None, *, umo: str) -> tuple[Any, str]:
        """挑一张表情并过闸门。

        Returns:
            ``(path 或 None, 拦截原因)``。path 为 None 表示没挑到或被限流。
        """
        limiter = getattr(self.stickers, "limiter", None)
        if limiter is not None:
            ok, reason = limiter.allow(umo)
            if not ok:
                return None, reason
        path = self.stickers.pick(category, rng=self._rng)
        return path, ""

    def apply_to_text(self, text: str, *, umo: str = "") -> tuple[str, list[Any]]:
        """给**没有事件对象**的场景（如主动消息）处理文本。

        主动消息直接走 ``context.send_message``，不经过 ``on_decorating_result``，
        因此必须在这条路径上单独做表情处理，否则模型写的 ``[sticker:x]``
        或残留的 ``[图片]`` 会字面发给用户。

        Returns:
            (清理后的文本, 要额外发送的图片组件列表)
        """
        cfg = self.config
        images: list[Any] = []
        if not text:
            return text, images

        if cfg.enable_stickers and not self.stickers.empty:

            def _sub(match):
                # 闸门：AI 主动写的标记也要限流，否则"主动要"就必发
                path, _ = self._pick_sticker(match.group(1), umo="")
                if path is not None and Image is not None:
                    images.append(Image.fromFileSystem(path))
                return ""

            text = STICKER_PATTERN.sub(_sub, text)
            if (
                not images
                and cfg.sticker_auto_probability > 0
                and self._rng.random() < cfg.sticker_auto_probability
            ):
                path = self._pick_sticker(None, umo="")[0]
                if path is not None and Image is not None:
                    images.append(Image.fromFileSystem(path))
        else:
            text = STICKER_PATTERN.sub("", text)

        # 没有配图时，清掉模型留下的悬空媒体占位符
        if not images:
            text = DANGLING_PLACEHOLDER_PATTERN.sub("", text)
        # 工具调用 XML 同样不该出现在正文里
        text = TOOL_CALL_XML_PATTERN.sub("", text)

        return text.strip(), images

    # ------------------------------------------------------------------
    def apply(self, event: Any) -> HumanizeResult:
        """就地修饰事件结果链。"""
        cfg = self.config
        result = HumanizeResult()

        try:
            message_result = event.get_result()
        except Exception:
            return result
        if message_result is None or not getattr(message_result, "chain", None):
            return result

        chain = list(message_result.chain)
        umo = str(getattr(event, "unified_msg_origin", "") or "")

        # 表情标记是我们给模型的指令，不是正文：无论能否换成图片，都必须从文本里
        # 消掉，否则用户会看到字面量 "[sticker:开心]"。
        if cfg.enable_stickers and not self.stickers.empty:
            chain, result = self._apply_stickers(chain, result, umo=umo)
        else:
            chain, result = self._strip_markers(chain, result)

        if cfg.enable_typos and Plain is not None:
            result.typo = self._apply_typos(chain)

        # 清掉模型留下的悬空媒体占位符（如 ``[图片]``）。
        # 真实图片是 Image 组件，字面量 "[图片]" 只可能是模型想配图却没配上，
        # 直接发出去用户会看到这个字面量。apply_to_text 一直有这步，事件路径
        # 此前漏了。
        chain = self._strip_placeholders(chain)

        message_result.chain = chain
        return result

    @staticmethod
    def _strip_placeholders(chain: list) -> list:
        """删除文本里残留的媒体占位符与工具调用 XML；清理后为空的段直接丢弃。"""
        if Plain is None:
            return chain
        out: list = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                cleaned = DANGLING_PLACEHOLDER_PATTERN.sub("", comp.text)
                # 工具调用 XML 绝不该出现在正文里（平台不解析它，会原样发出）
                cleaned = TOOL_CALL_XML_PATTERN.sub("", cleaned)
                # 顺带收掉清理后留下的多余空白/空行
                cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
                if cleaned:
                    comp.text = cleaned
                    out.append(comp)
                continue
            out.append(comp)
        return out

    def _strip_markers(self, chain: list, result: HumanizeResult) -> tuple[list, HumanizeResult]:
        """只删除标记、不插图（表情库为空或功能关闭时使用）。

        这里**不计入** ``replaced``——该字段表示「有多少标记换成了图片」；
        仅清理文本不是替换。
        """
        out: list = []
        for comp in chain:
            if isinstance(comp, Plain) and comp.text and STICKER_PATTERN.search(comp.text):
                cleaned = STICKER_PATTERN.sub("", comp.text)
                if cleaned.strip():
                    out.append(Plain(cleaned))
                # 清理后为空则整段丢弃
                continue
            out.append(comp)
        return out, result

    # ------------------------------------------------------------------
    def _apply_stickers(
        self, chain: list, result: HumanizeResult, *, umo: str = ""
    ) -> tuple[list, HumanizeResult]:
        """处理 [sticker:分类] 标记，并按概率补一张。"""
        cfg = self.config
        out: list = []

        for comp in chain:
            if not isinstance(comp, Plain) or not comp.text:
                out.append(comp)
                continue

            text = comp.text
            matches = list(STICKER_PATTERN.finditer(text))
            if not matches:
                out.append(comp)
                continue

            # 标记之间的纯文本保留，标记处插入图片
            cursor = 0
            for match in matches:
                head = text[cursor : match.start()]
                if head.strip():
                    out.append(Plain(head))
                # 闸门在 _pick_sticker 内部生效：被限流时返回 None，标记直接消失
                path, reason = self._pick_sticker(match.group(1), umo=umo)
                if path is not None and Image is not None:
                    out.append(Image.fromFileSystem(path))
                    result.replaced += 1
                elif reason:
                    result.blocked += 1
                    logger.debug(f"[ai_companion] 表情被限流：{reason}")
                cursor = match.end()
            tail = text[cursor:]
            if tail.strip():
                out.append(Plain(tail))

        # 概率补充：AI 没主动发表情时，偶尔自己来一张
        if (
            result.replaced == 0
            and cfg.sticker_auto_probability > 0
            and self._rng.random() < cfg.sticker_auto_probability
        ):
            path, reason = self._pick_sticker(None, umo=umo)
            if path is not None and Image is not None:
                out.append(Image.fromFileSystem(path))
                result.appended = True
            elif reason:
                logger.debug(f"[ai_companion] 自动表情被限流：{reason}")

        return out, result

    def _apply_typos(self, chain: list) -> bool:
        cfg = self.config
        changed = False
        for comp in chain:
            if not isinstance(comp, Plain) or not comp.text:
                continue
            new_text = apply_typos(
                comp.text,
                probability=cfg.typo_probability,
                rng=self._rng,
                max_typos=1,
            )
            if new_text != comp.text:
                comp.text = new_text
                changed = True
        return changed

    # ------------------------------------------------------------------
    @staticmethod
    def strip_sticker_markers(text: str) -> str:
        """把标记从文本里去掉（用于历史归档，避免存档里残留标记）。"""
        return STICKER_PATTERN.sub("", text).strip()

    def log_result(self, result: HumanizeResult) -> None:
        if not (result.replaced or result.appended or result.typo):
            return
        logger.debug(
            f"[ai_companion] 拟人化：表情替换 {result.replaced}、"
            f"自动表情 {result.appended}、错别字 {result.typo}"
        )
