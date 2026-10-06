"""拟人增强：错别字、表情包。

分段与打字延迟交给平台自带的「分段回复」机制（`respond/stage.py` 逐段按
对数间隔发送），本包不重复实现，避免双重延迟。
"""

from .humanizer import HumanizeResult, Humanizer
from .stickers import StickerLibrary, StickerRateLimiter
from .typo import apply_typos

__all__ = [
    "HumanizeResult",
    "Humanizer",
    "StickerLibrary",
    "StickerRateLimiter",
    "apply_typos",
]
