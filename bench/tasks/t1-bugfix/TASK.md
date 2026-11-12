A user reports that `top_words` in `textstats.py` returns words with the same count in a random-looking order.
Words with equal counts must be ordered alphabetically, for example `top_words("pear apple pear apple fig", 3)`
must return `[("apple", 2), ("pear", 2), ("fig", 1)]`. Fix the bug and add a test for it. Keep the existing tests passing.
