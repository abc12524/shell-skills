import unittest

import _bootstrap  # noqa: F401
from script import _edits


class EditPrimitiveTests(unittest.TestCase):
    def test_exact_replace(self):
        src = "def f():\n    return 1\n"
        out, count, err, code, meta = _edits._replace(src, "return 1", "return 2")
        self.assertEqual(out, "def f():\n    return 2\n")
        self.assertEqual(count, 1)
        self.assertIsNone(err)
        self.assertIsNone(meta)

    def test_fuzzy_indent(self):
        src = "def f():\n    return 1\n"
        # 锚点用 8 空格，实际文件是 4 空格 -> 走忽略缩进
        out, count, err, _code, meta = _edits._replace(src, "        return 1", "    return 2")
        self.assertIsNone(err)
        self.assertEqual(meta, {"matched": "stripped"})
        self.assertIn("return 2", out)

    def test_fuzzy_punctuation(self):
        src = "call('a', 'b')\n"
        out, count, err, _code, meta = _edits._replace(src, 'call("a", "b")', 'call("c")')
        self.assertIsNone(err)
        self.assertEqual(meta, {"matched": "normalized"})
        self.assertIn('call("c")', out)

    def test_multiple_matches_requires_all(self):
        src = "x = 1\nx = 1\n"
        out, count, err, code, _meta = _edits._replace(src, "x = 1", "x = 2")
        self.assertIsNone(out)
        self.assertEqual(code, "multiple_matches")
        out2, count2, err2, _code2, _meta2 = _edits._replace(src, "x = 1", "x = 2", replace_all=True)
        self.assertIsNone(err2)
        self.assertEqual(count2, 2)

    def test_rel_indent(self):
        text = "@0:def f():\n@1:return 1"
        out = _edits._apply_rel_indents(text, "", "    ")
        self.assertEqual(out, "def f():\n    return 1")

    def test_rel_indent_negative(self):
        out = _edits._apply_rel_indents("@-1:stmt", "        ", "    ")
        self.assertEqual(out, "    stmt")

    def test_indent_unit_gcd(self):
        self.assertEqual(_edits._indent_unit(["    a", "        b"]), "    ")
        self.assertEqual(_edits._indent_unit(["\ta"]), "\t")

    def test_tidy_seam_top_level(self):
        src = "def a():\n    pass\n\n\n\ndef b():\n    pass\n"
        out = _edits._tidy_seam(src, 3, top_gap=2)
        self.assertEqual(out, "def a():\n    pass\n\n\ndef b():\n    pass\n")

    def test_tidy_seam_member(self):
        src = "class A:\n    def a(self):\n        pass\n\n\n    def b(self):\n        pass\n"
        out = _edits._tidy_seam(src, 4, top_gap=2)
        self.assertIn("pass\n\n    def b", out)


if __name__ == "__main__":
    unittest.main()