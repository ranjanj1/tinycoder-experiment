"""Turn titles into URL slugs."""
import re
import unicodedata


def slugify(text, max_len=50):
    """Lower-case, ASCII-only slug made of words joined by single hyphens, at most max_len characters."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_len].rstrip("-")
