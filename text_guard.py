# text_guard.py — validation of AI-generated text before it is published.

_PAIRS = {'"': '"', "'": "'", "«": "»", "“": "”", "„": "“", "‘": "’"}


def clean_generated(text) -> str:
    """Strips whitespace and quotes that WRAP the whole text (not inner quotes)."""
    t = (text or "").strip()
    while len(t) >= 2 and _PAIRS.get(t[0]) == t[-1]:
        t = t[1:-1].strip()
    return t


def has_content(text: str, min_alnum: int) -> bool:
    """True if the text has at least `min_alnum` letters/digits ('!!!' or '...' fail)."""
    return sum(ch.isalnum() for ch in (text or "")) >= min_alnum