"""Small text statistics helpers."""
import re

WORD = re.compile(r"[a-z']+")


def words(text):
    """Lower-cased words in text, in order."""
    return WORD.findall(text.lower())


def word_counts(text):
    counts = {}
    for w in words(text):
        counts[w] = counts.get(w, 0) + 1
    return counts


def top_words(text, n=3):
    """The n most common words as (word, count) pairs, most common first."""
    counts = word_counts(text)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
