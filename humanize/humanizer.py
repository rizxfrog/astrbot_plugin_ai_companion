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


@dataclass
class HumanizeResult:
    replaced: int = 0
    appended: bool = False
    typo: bool = False


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

        # 表情标记是我们给模型的指令，不是正文：无论能否换成图片，都必须从文本里
        # 消掉，否则用户会看到字面量 "[sticker:开心]"。
        if cfg.enable_stickers and not self.stickers.empty:
            chain, result = self._apply_stickers(chain, result)
        else:
            chain, result = self._strip_markers(chain, result)

        if cfg.enable_typos and Plain is not None:
            result.typo = self._apply_typos(chain)

        message_result.chain = chain
        return result

    def _strip_markers(
        self, chain: list, result: HumanizeResult
    ) -> tuple[list, HumanizeResult]:
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
    def _apply_stickers(self, chain: list, result: HumanizeResult) -> tuple[list, HumanizeResult]:
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
                path = self.stickers.pick(
                    match.group(1), rng=self._rng
                )
                if path is not None and Image is not None:
                    out.append(Image.fromFileSystem(path))
                    result.replaced += 1
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
            path = self.stickers.pick(None, rng=self._rng)
            if path is not None and Image is not None:
                out.append(Image.fromFileSystem(path))
                result.appended = True

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
