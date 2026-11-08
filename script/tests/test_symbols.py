import unittest

import _bootstrap  # noqa: F401
from script import _symbols


class SymbolTests(unittest.TestCase):
    def test_decorated_class_includes_decorator(self):
        code = "@decorator\nclass A:\n    def m(self):\n        pass\n"
        parsed = _symbols.PythonParser(code).parse()
        cls = next(s for s in parsed.symbols if s.name == "A")
        self.assertEqual(cls.start, 1)
        self.assertEqual(cls.end, 4)
        self.assertEqual(cls.body_start, 3)

    def test_nested_class(self):
        code = "class Outer:\n    class Inner:\n        def m(self):\n            pass\n"
        parsed = _symbols.PythonParser(code).parse()
        names = [s.name for s in parsed.symbols]
        self.assertIn("Outer", names)
        self.assertIn("Outer.Inner", names)
        self.assertIn("Outer.Inner.m", names)
        self.assertIn("Outer.Inner.m", parsed.calls)

    def test_nested_method_call_graph(self):
        code = "class A:\n    def run(self):\n        return helper()\ndef helper():\n    return 1\n"
        parsed = _symbols.PythonParser(code).parse()
        self.assertEqual(parsed.calls["A.run"], {"helper"})

    def test_go_symbols(self):
        code = ("package main\n"
                "import \"fmt\"\n"
                "type Server struct {\n    name string\n}\n"
                "func (s *Server) Start(port int) error {\n    fmt.Println(port)\n    return nil\n}\n"
                "func main() {\n    var s Server\n    s.Start(1)\n}\n")
        parsed = _symbols.get_parser(code, "go").parse()
        names = [s.name for s in parsed.symbols]
        self.assertIn("Server", names)
        self.assertIn("Start", names)
        self.assertIn("main", names)
        self.assertIn("fmt", parsed.imports)

    def test_rust_symbols_and_raw_string(self):
        code = ("use std::fmt;\n"
                "pub struct Point { x: i32 }\n"
                "impl Point {\n"
                "    pub fn new() -> Self { Point { x: 0 } }\n"
                "}\n"
                "fn main() {\n"
                "    let s = r#\"fn fake() { \"#;\n"
                "    println!(\"{}\", s);\n"
                "}\n")
        parsed = _symbols.get_parser(code, "rust").parse()
        names = [s.name for s in parsed.symbols]
        self.assertIn("Point", names)
        self.assertIn("Point.new", names)
        self.assertIn("main", names)
        # raw string 里的假函数不能被登记
        self.assertNotIn("fake", names)
        self.assertIn("std::fmt", parsed.imports)

    def test_c_preprocessor_uses_first_branch(self):
        code = ("#ifdef A\n"
                "int f() {\n"
                "    return 1;\n"
                "}\n"
                "#else\n"
                "int g() {\n"
                "    return 2;\n"
                "}\n"
                "#endif\n"
                "int h() {\n"
                "    return 3;\n"
                "}\n")
        parsed = _symbols.get_parser(code, "c").parse()
        names = [s.name for s in parsed.symbols]
        self.assertIn("f", names)
        self.assertIn("h", names)

    def test_validate_source_python(self):
        self.assertEqual(_symbols.validate_source("def f():\n    return 1\n", "python"),
                         (True, None))
        ok, msg = _symbols.validate_source("def f(:\n", "python")
        self.assertFalse(ok)
        self.assertIn("第", msg)

    def test_validate_source_brace(self):
        ok, _msg = _symbols.validate_source("function f() { return 1; }", "javascript")
        self.assertTrue(ok)
        ok2, msg2 = _symbols.validate_source("function f() { return 1;", "javascript")
        self.assertFalse(ok2)
        self.assertIn("未闭合", msg2)

    def test_plain_language(self):
        self.assertTrue(_symbols._is_plain("markdown"))
        self.assertTrue(_symbols._is_plain(None))
        self.assertFalse(_symbols._is_plain("rust"))


if __name__ == "__main__":
    unittest.main()