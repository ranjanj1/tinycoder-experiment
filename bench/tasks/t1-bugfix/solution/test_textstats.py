import unittest

from textstats import top_words, word_counts, words


class TextStatsTest(unittest.TestCase):
    def test_words(self):
        self.assertEqual(words("The cat's hat."), ["the", "cat's", "hat"])

    def test_counts(self):
        self.assertEqual(word_counts("a b a"), {"a": 2, "b": 1})

    def test_top(self):
        self.assertEqual(top_words("b b b a a c", 2), [("b", 3), ("a", 2)])


if __name__ == "__main__":
    unittest.main()
