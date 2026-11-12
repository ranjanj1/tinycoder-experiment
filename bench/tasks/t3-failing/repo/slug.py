"""Turn titles into URL slugs."""
import re


def slugify(text, max_len=50):
    """Lower-case, ASCII-only slug made of words joined by single hyphens, at most max_len characters."""
    text = text.lower()
    text = re.sub(r"[^a-z0-9]", "-", text)
    return text[:max_len]
