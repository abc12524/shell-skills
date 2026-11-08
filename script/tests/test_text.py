import os
import tempfile
import unittest

import _bootstrap  # noqa: F401
from script import _text


class TextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_utf16_bom_is_text_not_binary(self):
        path = self._path("u16.txt")
        with open(path, "wb") as f:
            f.write("hello\nworld\n".encode("utf-16"))
        text, enc, newline, err = _text.read_file(path)
        self.assertIsNone(err)
        self.assertIn(enc, ("utf-16", "utf-16-le", "utf-16-be"))
        self.assertEqual(text, "hello\nworld\n")

    def test_utf8_sig_roundtrip(self):
        path = self._path("bom.txt")
        with open(path, "wb") as f:
            f.write("caf\u00e9\n".encode("utf-8-sig"))
        text, enc, _newline, err = _text.read_file(path)
        self.assertIsNone(err)
        self.assertEqual(text, "caf\u00e9\n")
        self.assertEqual(enc, "utf-8-sig")

    def test_binary_rejected(self):
        path = self._path("bin.dat")
        with open(path, "wb") as f:
            f.write(b"\x00\x01\x02\x03" * 64)
        text, _enc, _nl, err = _text.read_file(path)
        self.assertIsNone(text)
        self.assertIn("二进制", err)

    def test_atomic_write_preserves_permissions(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限位在 Windows 上无意义")
        path = self._path("exec.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)
        err = _text.write_file(path, "#!/bin/sh\necho hi\n", "utf-8", "\n")
        self.assertIsNone(err)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o755)

    def test_symlink_rejected(self):
        real = self._path("real.txt")
        link = self._path("link.txt")
        with open(real, "w", encoding="utf-8") as f:
            f.write("data\n")
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            self.skipTest("当前平台不允许创建符号链接")
        err = _text.write_file(link, "new\n", "utf-8", "\n")
        self.assertIn("软链接", err)

    def test_sha_changes_after_write(self):
        path = self._path("hash.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("one\n")
        first = _text.file_sha256(path)
        _text.write_file(path, "two\n", "utf-8", "\n")
        self.assertNotEqual(first, _text.file_sha256(path))

    def test_large_file_head(self):
        path = self._path("big.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("line\n" * 4096)
        text, _enc, _nl, err = _text.read_file(path, max_bytes=64)
        self.assertIsNone(text)
        self.assertIn("文件过大", err)
        head, _enc2, err2 = _text.read_file_head(path, 64)
        self.assertIsNone(err2)
        self.assertLess(len(head), 100)

    def test_short_path_keeps_relative_levels(self):
        base = self.tmp.name
        path = os.path.join(base, "pkg", "mod.py")
        os.makedirs(os.path.dirname(path))
        shown = _text._short_path(path, cwd=base)
        self.assertEqual(shown, "pkg/mod.py")

    def test_short_path_outside_cwd_falls_back_to_basename(self):
        nested = os.path.join(self.tmp.name, "sub")
        os.makedirs(nested)
        path = os.path.join(self.tmp.name, "x.py")
        self.assertEqual(_text._short_path(path, cwd=nested), "x.py")


if __name__ == "__main__":
    unittest.main()