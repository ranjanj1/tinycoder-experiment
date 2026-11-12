import unittest

from slug import slugify


class HiddenT3(unittest.TestCase):
    def test_mixed(self):
        self.assertEqual(slugify("  Ünïcode -- Tëst!  "), "unicode-test")

    def test_digits(self):
        self.assertEqual(slugify("Top 10 Tips (2026)"), "top-10-tips-2026")

    def test_only_symbols(self):
        self.assertEqual(slugify("!!!"), "")

    def test_truncate_never_ends_with_hyphen(self):
        for n in range(1, 20):
            s = slugify("alpha beta gamma delta", max_len=n)
            self.assertLessEqual(len(s), n)
            self.assertFalse(s.endswith("-") or s.startswith("-"), s)

    def test_default_length(self):
        self.assertLessEqual(len(slugify("word " * 30)), 50)

    def test_already_slug(self):
        self.assertEqual(slugify("already-a-slug"), "already-a-slug")


if __name__ == "__main__":
    unittest.main()
