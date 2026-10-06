"""本地表情包库。

约定目录结构（放在插件数据目录下）：:

    stickers/
    ├── 开心/    😄.jpg
    ├── 无语/    ....gif
    ├── 通用/    hi.png

**子目录名即情绪分类**；直接放在根目录下的图片归入 ``通用``。
AI 在回复里写 ``[sticker:开心]`` 时，从对应分类里随机挑一张发出。
"""

from __future__ import annotations

import random
import time
from pathlib import Path

from astrbot.api import logger

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DEFAULT_CATEGORY = "通用"


class StickerRateLimiter:
    """表情发送闸门：卡在「真正要发出去」这一刻。

    表情有三条发送路径 —— ① AI 写 ``[sticker:x]`` 标记、② AI 调用
    ``send_sticker`` 工具、③ 代码按概率自动补图。只在某一条上做概率限制会漏掉
    其余两条，导致「AI 主动要就必发」。因此把限制收口到本类，三条路径共用同一
    个闸门，才能真正控制整体频率。

    两条独立规则，任一命中即拦截：

    * ``drop_rate`` —— 单张放行概率。默认 0.2，即**拦掉 80%**。
    * ``cooldown_seconds`` —— 距上一张成功发出的最小间隔，避免连续多段回复
      里每段都带表情。

    只统计**成功发出的**表情：被拦下的不算，否则一次拦截会把冷却窗口一并
    推进，反而让后续更容易发。
    """

    def __init__(
        self,
        *,
        drop_rate: float = 0.2,
        cooldown_seconds: int = 0,
        rng: random.Random | None = None,
    ) -> None:
        self.drop_rate = max(0.0, min(1.0, float(drop_rate)))
        self.cooldown_seconds = max(0, int(cooldown_seconds))
        self._rng = rng or random.Random()
        self._last_sent: dict[str, float] = {}
        self.blocked = 0
        self.allowed = 0

    def allow(self, umo: str, *, now: float | None = None) -> tuple[bool, str]:
        """判断这张表情是否可以发出。

        Returns:
            ``(是否放行, 原因)``；``reason`` 仅在被拦截时用于日志。
        """
        now = time.time() if now is None else now

        if self.cooldown_seconds > 0:
            last = self._last_sent.get(umo)
            if last is not None and now - last < self.cooldown_seconds:
                self.blocked += 1
                return False, f"冷却中（距上张 {now - last:.0f}s < {self.cooldown_seconds}s）"

        if self.drop_rate < 1.0 and self._rng.random() >= self.drop_rate:
            self.blocked += 1
            return False, f"概率未命中（放行率 {self.drop_rate:.2f}）"

        self.allowed += 1
        self._last_sent[umo] = now
        return True, ""


class StickerLibrary:
    """扫描并随机抽取本地表情包。"""

    def __init__(self, roots: list[Path]) -> None:
        self._roots = [Path(r) for r in roots]
        self._index: dict[str, list[Path]] = {}
        # 三条发送路径共用的闸门，配置在插件启动时注入
        self.limiter: StickerRateLimiter | None = None
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        """重新扫描磁盘（新增表情包后无需重启）。"""
        index: dict[str, list[Path]] = {}
        for root in self._roots:
            if not root.exists():
                continue
            # 根目录下的图片 -> 通用
            for item in root.iterdir():
                if item.is_file() and item.suffix.lower() in SUPPORTED_EXTS:
                    index.setdefault(DEFAULT_CATEGORY, []).append(item)
                elif item.is_dir():
                    files = [
                        f
                        for f in item.iterdir()
                        if f.is_file() and f.suffix.lower() in SUPPORTED_EXTS
                    ]
                    if files:
                        index.setdefault(item.name, []).extend(files)
        self._index = index
        if index:
            logger.info(
                "[ai_companion] 已加载表情包："
                + "，".join(f"{k}({len(v)})" for k, v in sorted(index.items()))
            )

    # ------------------------------------------------------------------
    @property
    def empty(self) -> bool:
        return not self._index

    def categories(self) -> list[str]:
        return sorted(self._index.keys())

    def all_category(self) -> str:
        return DEFAULT_CATEGORY

    def pick(self, category: str | None = None, *, rng: random.Random | None = None) -> Path | None:
        """随机取一张表情包。

        ``category`` 为空或不存在时，退回到「通用 + 全部分类」的合并池，
        保证 AI 想要表情时总能拿到一张。
        """
        rng = rng or random
        category = (category or "").strip()

        if category and category in self._index:
            pool = self._index[category]
        elif DEFAULT_CATEGORY in self._index:
            pool = self._index[DEFAULT_CATEGORY]
        else:
            pool = [f for files in self._index.values() for f in files]

        if not pool:
            return None
        return rng.choice(pool)

    def resolve_category(self, requested: str) -> str:
        """把 AI 写的分类名落到实际存在的分类上（容忍空/未知）。"""
        requested = (requested or "").strip()
        if requested in self._index:
            return requested
        return (
            DEFAULT_CATEGORY
            if DEFAULT_CATEGORY in self._index
            else (self.categories()[0] if self._index else "")
        )
