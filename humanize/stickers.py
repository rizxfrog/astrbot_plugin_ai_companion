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
from pathlib import Path

from astrbot.api import logger

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
DEFAULT_CATEGORY = "通用"


class StickerLibrary:
    """扫描并随机抽取本地表情包。"""

    def __init__(self, roots: list[Path]) -> None:
        self._roots = [Path(r) for r in roots]
        self._index: dict[str, list[Path]] = {}
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

    def pick(
        self, category: str | None = None, *, rng: random.Random | None = None
    ) -> Path | None:
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
        return DEFAULT_CATEGORY if DEFAULT_CATEGORY in self._index else (
            self.categories()[0] if self._index else ""
        )
