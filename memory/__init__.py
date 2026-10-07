"""记忆层：短期记忆压缩、人物与关系抽取、群画像与熟悉度。"""

from .compactor import Compactor, CompactResult
from .group_profile import (
    STAGE_THRESHOLDS,
    Familiarity,
    GroupProfileManager,
    compute_familiarity,
    familiarity_stage,
)
from .knowledge import ExtractionResult, KnowledgeExtractor

__all__ = [
    "STAGE_THRESHOLDS",
    "CompactResult",
    "Compactor",
    "ExtractionResult",
    "Familiarity",
    "GroupProfileManager",
    "KnowledgeExtractor",
    "compute_familiarity",
    "familiarity_stage",
]
