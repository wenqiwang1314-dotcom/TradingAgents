import re
from typing import Optional

from tradingagents.llm_clients.base_client import strip_reasoning_traces


RATING_SCALE = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
RATING_WORDS = {rating.upper(): rating for rating in RATING_SCALE}
ACTION_BY_RATING = {
    "Buy": "buy",
    "Overweight": "buy",
    "Hold": "hold",
    "Underweight": "sell",
    "Sell": "sell",
}

RATING_SCALE_PROMPT = """**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Trade Action Mapping**:
- Buy / Overweight -> Trade Action: BUY
- Hold -> Trade Action: HOLD
- Underweight / Sell -> Trade Action: SELL"""

_RATING_VALUE = r"BUY|OVERWEIGHT|HOLD|UNDERWEIGHT|SELL"
_LABELED_RATING_RE = re.compile(
    rf"\b(?:RATING|RECOMMENDATION|FINAL\s+RATING)\**\s*[:\-]\s*\**\s*({_RATING_VALUE})\s*\**\b",
    re.IGNORECASE,
)
_ANY_RATING_RE = re.compile(rf"\b({_RATING_VALUE})\b", re.IGNORECASE)


def normalize_rating(value: str) -> Optional[str]:
    """Return canonical title-case rating or None when value is not a rating."""
    if not value:
        return None
    return RATING_WORDS.get(str(value).strip().strip("*").upper())


def extract_rating(text: str) -> Optional[str]:
    """Extract a canonical five-point rating from free-form text."""
    cleaned = strip_reasoning_traces(text or "")
    labeled = _LABELED_RATING_RE.findall(cleaned)
    if labeled:
        return normalize_rating(labeled[-1])

    any_rating = _ANY_RATING_RE.findall(cleaned)
    if any_rating:
        return normalize_rating(any_rating[-1])
    return None


def action_from_rating(rating: str) -> str:
    """Map canonical rating to executable action: buy, sell, or hold."""
    canonical = normalize_rating(rating)
    if not canonical:
        return "hold"
    return ACTION_BY_RATING[canonical]


def action_from_signal(signal: str) -> str:
    """Extract a rating from signal text and map it to executable action."""
    rating = extract_rating(signal)
    return action_from_rating(rating or "")
