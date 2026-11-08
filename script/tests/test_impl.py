import os
import tempfile
import time
import unittest

import _bootstrap  # noqa: F401
from script._script_impl import (_DeadlineExceeded, _check_deadline, _deadline,
                                 script_editor)
from script.skill import run


class ScriptEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name, text, enc="utf-8"):
        path = os.path.join(self.tmp.name, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding=enc, newline="") as f:
            f.write(text)
        return path

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_read_symbol(self):
        path = self._write("a.py", "def f():\n    return 1\n")
        result = script_editor(action="read", file_path=path, symbol="f")
        self.assertEqual(result["status"], "ok")
        body = result["result"]
        self.assertIn("# f (function) L1-2", body["result"])
        self.assertIn("return 1", body["result"])
        self.assertIn("file_id", body)

    def test_outline(self):
        path = self._write("a.py", "class A:\n    def m(self):\n        pass\n")
        result = script_editor(action="outline", file_path=path)
        self.assertEqual(result["status"], "ok")
        self.assertIn("class A", result["result"]["result"])

    def test_stale_sha_rejected(self):
        path = self._write("a.py", "x = 1\n")
        first = script_editor(action="read", file_path=path)["result"]["file_id"]
        self._write("a.py", "x = 2\n")
        result = script_editor(action="edit", file_path=path, old_code="x = 2",
                               new_code="x = 3", expected_sha256=first)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "stale_file")
        self.assertEqual(self._read(path), "x = 2\n")

    def test_validate_auto_refuses_broken_edit(self):
        path = self._write("a.py", "def f():\n    return 1\n")
        result = script_editor(action="edit", file_path=path, old_code="return 1",
                               new_code="return (")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "parse_failed")
        self.assertEqual(self._read(path), "def f():\n    return 1\n")

    def test_validate_off_allows_broken_edit(self):
        path = self._write("a.py", "def f():\n    return 1\n")
        result = script_editor(action="edit", file_path=path, old_code="return 1",
                               new_code="return (", validate="off")
        self.assertEqual(result["status"], "ok")
        self.assertIn("return (", self._read(path))

    def test_create_checks_syntax(self):
        path = os.path.join(self.tmp.name, "bad.py")
        result = script_editor(action="create", file_path=path, new_code="def f(:\n",
                               language="python")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "parse_failed")
        self.assertFalse(os.path.exists(path))

    def test_read_symbol_list(self):
        path = self._write("a.py", "def a():\n    pass\n\ndef b():\n    pass\n")
        result = script_editor(action="read", file_path=path, symbol=["a", "b"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("# a (function)", result["result"]["result"])
        self.assertIn("# b (function)", result["result"]["result"])

    def test_outline_with_calls(self):
        path = self._write("a.py", "def a():\n    return b()\n\ndef b():\n    return 1\n")
        result = script_editor(action="outline", file_path=path, with_calls=True)
        self.assertEqual(result["status"], "ok")
        self.assertIn("calls:", result["result"]["result"])
        self.assertIn("a -> b", result["result"]["result"])

    def test_gbk_encoding_roundtrip(self):
        path = os.path.join(self.tmp.name, "gbk.txt")
        with open(path, "wb") as f:
            f.write("中文内容\n".encode("gb18030"))
        result = script_editor(action="edit", file_path=path,
                               old_code="中文内容", new_code="中文内容2")
        self.assertEqual(result["status"], "ok", result)
        with open(path, "rb") as f:
            self.assertEqual(f.read().decode("gb18030"), "中文内容2\n")

    def test_patch_single_file_top_level(self):
        path = self._write("a.py", "x = 1\n")
        result = script_editor(action="patch", file_path=path,
                               edits=[{"old_code": "x = 1", "new_code": "x = 2"}])
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(self._read(path), "x = 2\n")

    def test_patch_multiple_files_and_rename(self):
        a = self._write("a.py", "def a():\n    return 1\n")
        b = self._write("b.py", "def b():\n    return 2\n")
        a2 = os.path.join(self.tmp.name, "renamed.py")
        result = script_editor(action="patch", files=[
            {"file_path": a, "edits": [{"old_code": "return 1", "new_code": "return 10"}],
             "rename_to": a2},
            {"file_path": b, "edits": [{"old_code": "return 2", "new_code": "return 20"}]},
        ])
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["result"]["files_changed"], 2)
        self.assertFalse(os.path.exists(a))
        self.assertIn("return 10", self._read(a2))
        self.assertIn("return 20", self._read(b))

    def test_patch_rolls_back_on_failure(self):
        a = self._write("a.py", "def a():\n    return 1\n")
        b = self._write("b.py", "def b():\n    return 2\n")
        result = script_editor(action="patch", files=[
            {"file_path": a, "edits": [{"old_code": "return 1", "new_code": "return 10"}]},
            {"file_path": b, "edits": [{"old_code": "missing text", "new_code": "x"}]},
        ])
        self.assertEqual(result["status"], "error")
        # 失败发生在落盘前：a 不应被修改
        self.assertEqual(self._read(a), "def a():\n    return 1\n")

    def test_patch_rolls_back_written_files(self):
        from unittest import mock
        from script import _script_impl
        a = self._write("a.py", "def a():\n    return 1\n")
        b = self._write("b.py", "def b():\n    return 2\n")
        real = _script_impl.write_file
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                return "模拟第二次写入失败"
            return real(*args, **kwargs)

        with mock.patch.object(_script_impl, "write_file", side_effect=flaky):
            result = script_editor(action="patch", files=[
                {"file_path": a, "edits": [{"old_code": "return 1", "new_code": "return 10"}]},
                {"file_path": b, "edits": [{"old_code": "return 2", "new_code": "return 20"}]},
            ])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self._read(a), "def a():\n    return 1\n")
        self.assertEqual(self._read(b), "def b():\n    return 2\n")

    def test_rename_symbol_python(self):
        path = self._write("a.py",
                           'def greet(name):\n    return "hi " + name\n\n'
                           'def main():\n    return greet("x")\n')
        result = script_editor(action="rename_symbol", file_path=path,
                               symbol="greet", new_name="hello")
        self.assertEqual(result["status"], "ok", result)
        self.assertEqual(result["result"]["renamed"], 2)
        body = self._read(path)
        self.assertIn("def hello(", body)
        self.assertIn('return hello("x")', body)
        self.assertIn('"hi "', body)          # 字符串内容不受影响

    def test_rename_symbol_typescript(self):
        path = self._write("a.ts",
                           "export function greet(x) { return helper(x); }\n"
                           "function helper(y) { return y; }\n")
        result = script_editor(action="rename_symbol", file_path=path,
                               symbol="greet", new_name="hello")
        self.assertEqual(result["status"], "ok", result)
        body = self._read(path)
        self.assertIn("function hello", body)
        self.assertNotIn("function greet", body)
        self.assertIn("function helper", body)

    def test_deadline_check(self):
        deadline = _deadline(1)
        time.sleep(0.01)
        with self.assertRaises(_DeadlineExceeded):
            _check_deadline(deadline)

    def test_unknown_argument_rejected(self):
        result = run({"action": "read", "bogus": 1})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "unknown_argument")


if __name__ == "__main__":
    unittest.main()