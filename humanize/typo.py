"""错别字模拟。

真人打字不会永远正确。这里用一张**保守的**混淆表制造自然的错别字：
只把常见同音/形近字替换掉，绝不碰标点、数字、链接、表情标记与英文。

之所以不用拼音库：引入一个词典级依赖只为 2% 的概率不值得，而中文最常见的
手误本就集中在少数高频字上（的/得/地、在/再、有/又…），手工表足够真实。
"""

from __future__ import annotations

import random

# 高频手误对照表：key -> 可能的误写（同音或形近）
CONFUSIONS: dict[str, tuple[str, ...]] = {
    "的": ("得", "地"),
    "得": ("的",),
    "地": ("的",),
    "在": ("再",),
    "再": ("在",),
    "有": ("又",),
    "又": ("有",),
    "很": ("狠",),
    "那": ("哪",),
    "哪": ("那",),
    "是": ("事",),
    "事": ("是",),
    "我": ("找",),
    "你": ("拟",),
    "吗": ("嘛",),
    "嘛": ("吗",),
    "吧": ("把",),
    "把": ("吧",),
    "做": ("作",),
    "作": ("做",),
    "以": ("已",),
    "已": ("以",),
    "想": ("响",),
    "知": ("之",),
    "道": ("到",),
    "到": ("道",),
    "说": ("悦",),
    "过": ("锅",),
    "没": ("每",),
    "会": ("回",),
    "好": ("号",),
    "了": ("啦",),
    "啦": ("了",),
}

# 永不改动的字符：标点、空白、数字、表情标记里的符号
_SAFE_PUNCT = set("，。！？、；：''（）【】《》…—～,.!?;:()[]{}<>\"'`~ \n\r\t")
_MARKER_START = "[sticker"


def should_typo(probability: float, rng: random.Random) -> bool:
    return probability > 0 and rng.random() < probability


def apply_typos(
    text: str,
    *,
    probability: float,
    rng: random.Random,
    max_typos: int = 1,
) -> str:
    """按概率给文本制造最多 ``max_typos`` 个错别字。

    probability 是「整段文本出一次错」的概率，与长度无关——这样长回复不会因为
    字多就错得离谱。
    """
    if not text or not should_typo(probability, rng):
        return text

    # 找出候选位置（跳过标点/数字/英文/表情标记）
    candidates: list[int] = []
    marker_spans = _marker_spans(text)
    for idx, ch in enumerate(text):
        if ch in CONFUSIONS and not _in_spans(idx, marker_spans):
            candidates.append(idx)
    if not candidates:
        return text

    rng.shuffle(candidates)
    chars = list(text)
    made = 0
    for idx in candidates:
        if made >= max_typos:
            break
        options = CONFUSIONS.get(chars[idx])
        if not options:
            continue
        chars[idx] = rng.choice(options)
        made += 1

    return "".join(chars)


def _marker_spans(text: str) -> list[tuple[int, int]]:
    """找出 [sticker:xxx] 标记的区间，避免把标记里的字改坏。"""
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        begin = text.find(_MARKER_START, start)
        if begin == -1:
            break
        end = text.find("]", begin)
        if end == -1:
            break
        spans.append((begin, end))
        start = end + 1
    return spans


def _in_spans(index: int, spans: list[tuple[int, int]]) -> bool:
    return any(begin <= index <= end for begin, end in spans)
