#!/usr/bin/env python3
"""脚本类工具：script_editor —— 面向文件的脚本 增 / 删 / 改 / 查（CRUD）+ 结构骨架

数据来源只支持 file_path（本地文件）或 url（http/https，只读），由工具自行读取与编码探测，
输出统一 UTF-8；本地文件修改会写回原路径（原子写入：临时文件 + os.replace）。

- 编码自动探测（utf-8 / BOM / gb18030 / big5 / latin-1 兜底），换行统一为 '\\n'，写回还原原换行；
- read：symbol（单个/多个，支持裸方法名）/ pattern（正则，先匹配符号名再回退按行 grep）/
        start_line~end_line 行范围，输出带绝对行号；
- outline：结构骨架，每个符号带起止行号；默认不含调用关系（with_calls=true 才输出）；
- add/edit/delete 支持三种定位：symbol（+position）> start_line 行号 > old_code 文本锚点；
  插入自动补齐上下文缩进；
- 输出统一限长（limit），截断时给出可直接复用的 next 参数，避免整体被截；
- 编辑采用「精确字符串替换」：唯一匹配才执行，多处匹配需 replace_all=true；
  old_code 未命中时回显最接近的行号候选，便于二次精确命中。

语言：python / java / kotlin / c / cpp / csharp / javascript / typescript / shell
"""
import ast
import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse
from urllib.request import urlopen

try:  # android-agent / kelivo-agent：技能目录部署了 _envelope.py
    from _envelope import ok, error
except ImportError:  # shell-tool：使用 core.tools.envelope
    from core.tools.envelope import ok, error


# ============================ 结构解析（供 outline / symbol 定位） ============================

@dataclass
class SymbolRange:
    """一个可定位符号的范围（1-based，含装饰器/注解）"""
    name: str
    kind: str          # function | class | method | interface | enum | type | ...
    start: int
    end: int
    signature: str     # 单行签名，用于骨架输出
    indent: str = ""       # 符号自身缩进
    body_start: int = 0    # 符号体首行（1-based）；无体则等于 start
    body_end: int = 0      # 符号体末行（1-based）；无体则等于 end


@dataclass
class ParseResult:
    language: str
    lines: int
    imports: List[str]
    globals: List[str]
    symbols: List[SymbolRange]
    calls: Dict[str, Set[str]] = field(default_factory=dict)


class PythonParser:
    """Python 解析器：使用 ast，得到最精确的符号范围与调用关系"""

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines()
        self.tree = ast.parse(code)

    def parse(self) -> ParseResult:
        imports: List[str] = []
        globals_: List[str] = []
        symbols: List[SymbolRange] = []
        calls: Dict[str, Set[str]] = {}

        for node in self.tree.body:
            if isinstance(node, ast.Import):
                imports += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                imports.append(f"{mod}({','.join(a.name for a in node.names)})")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        globals_.append(t.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(self._func_sym(node, ""))
                calls[node.name] = self._calls_of(node)
            elif isinstance(node, ast.ClassDef):
                symbols.append(self._class_sym(node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append(self._func_sym(child, node.name, kind="method"))
                        calls[f"{node.name}.{child.name}"] = self._calls_of(child)

        return ParseResult("python", len(self.lines), imports, globals_, symbols, calls)

    @staticmethod
    def _start_line(node: ast.AST) -> int:
        decos = getattr(node, "decorator_list", None)
        if decos:
            return min(d.lineno for d in decos)
        return node.lineno

    def _indent_of(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return re.match(r"[ \t]*", self.lines[lineno - 1]).group(0)
        return ""

    @staticmethod
    def _body_bounds(node: ast.AST) -> Tuple[int, int]:
        body = getattr(node, "body", None)
        if not body:
            return node.lineno, node.end_lineno
        return PythonParser._start_line(body[0]), body[-1].end_lineno

    def _func_sym(self, node: ast.AST, cls: str, kind: str = "function") -> SymbolRange:
        args = ast.unparse(node.args) if getattr(node, "args", None) else ""
        ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
        name = f"{cls}.{node.name}" if cls else node.name
        sig = f"{name}({args}){ret}"
        start = self._start_line(node)
        bs, be = self._body_bounds(node)
        return SymbolRange(name, kind, start, node.end_lineno, sig,
                           indent=self._indent_of(node.lineno), body_start=bs, body_end=be)

    def _class_sym(self, node: ast.AST) -> SymbolRange:
        bases = ", ".join(ast.unparse(b) for b in node.bases)
        sig = f"class {node.name}" + (f"({bases})" if bases else "")
        bs, be = self._body_bounds(node)
        return SymbolRange(node.name, "class", node.lineno, node.end_lineno, sig,
                           indent=self._indent_of(node.lineno), body_start=bs, body_end=be)

    @staticmethod
    def _calls_of(node: ast.AST) -> Set[str]:
        out: Set[str] = set()
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    out.add(n.func.id)
                elif isinstance(n.func, ast.Attribute):
                    out.add(n.func.attr)
        out.discard(getattr(node, "name", None))
        return out


class BraceParser:
    """大括号语言通用解析器基类：正则匹配签名 + 括号深度感知扫描求结束行

    子类覆盖 LANG / TYPE_PATTERNS / FUNC_PATTERNS / MEMBER_PATTERNS / IMPORT_PATTERNS。

    扫描规则（避免「无体声明吞掉下一个函数」）：
    - 从签名行起按字符扫描，圆括号深度归零后遇到的第一个 '{' 视为体开始，配对到 '}'；
    - 深度为零时遇到 ';'，或签名闭合后下一非空行不以 '{' 开头（表达式体 / 声明），
      则按单行符号处理；
    - '{' 在下一行（Allman 风格，C#/Java 常见）也能正确配对。
    """

    LANG = "text"
    TYPE_PATTERNS: List[Tuple[str, str]] = []
    FUNC_PATTERNS: List[str] = []
    MEMBER_PATTERNS: List[str] = []
    IMPORT_PATTERNS: List[str] = []
    CONTROL = {"if", "elif", "else", "for", "foreach", "while", "do", "switch", "case",
               "catch", "try", "finally", "return", "throw", "throws", "new", "delete",
               "using", "lock", "with", "await", "yield", "var", "sizeof", "typedef",
               "namespace", "echo", "local", "readonly", "super", "this",
               "then", "fi", "done", "esac", "eval", "exec", "exit", "shift"}

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines()
        # 屏蔽注释与字符串，避免 "// class Foo" 之类误匹配；保持行号不变
        self.clean = self._mask_comments_and_strings(code)
        self.clean_lines = self.clean.splitlines()

    @staticmethod
    def _mask_comments_and_strings(code: str) -> str:
        out: List[str] = []
        i, n = 0, len(code)
        in_line = in_block = False
        quote: Optional[str] = None
        while i < n:
            c = code[i]
            nxt = code[i + 1] if i + 1 < n else ""
            if in_line:
                if c == "\n":
                    in_line = False
                    out.append(c)
                else:
                    out.append(" ")
                i += 1
                continue
            if in_block:
                if c == "*" and nxt == "/":
                    in_block = False
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if c == "\n" else " ")
                i += 1
                continue
            if quote:
                if c == "\\":
                    out.append("  ")
                    i += 2
                    continue
                if c == quote:
                    quote = None
                out.append("\n" if c == "\n" else " ")
                i += 1
                continue
            if c == "/" and nxt == "/":
                in_line = True
                out.append("  ")
                i += 2
                continue
            if c == "/" and nxt == "*":
                in_block = True
                out.append("  ")
                i += 2
                continue
            if c in ('"', "'", "`"):
                quote = c
                out.append(" ")
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)

    # ---------------- 扫描 ----------------

    def _next_nonblank(self, i: int) -> Optional[int]:
        for j in range(i + 1, len(self.clean_lines)):
            if self.clean_lines[j].strip():
                return j
        return None

    def _scan(self, i: int) -> Tuple[int, bool, Optional[int]]:
        """从第 i 行（0-based）扫描签名与体，返回 (结束行, 是否有块, '{' 所在行)"""
        n = len(self.clean_lines)
        depth = 0
        seen_paren = False
        j = i
        while j < n:
            line = self.clean_lines[j]
            for k, c in enumerate(line):
                if c == "(":
                    depth += 1
                    seen_paren = True
                elif c == ")":
                    depth -= 1
                elif depth <= 0:
                    if c == "{":
                        return self._pair(j, k)
                    if c == ";":
                        return j, False, None
            if seen_paren and depth <= 0:
                nxt = self._next_nonblank(j)
                if nxt is None or not self.clean_lines[nxt].lstrip().startswith("{"):
                    return j, False, None
                j = nxt
                continue
            j += 1
        return n - 1, False, None

    def _pair(self, j: int, k: int) -> Tuple[int, bool, Optional[int]]:
        depth = 0
        for i in range(j, len(self.clean_lines)):
            start = k if i == j else 0
            for c in self.clean_lines[i][start:]:
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return i, True, j
        return len(self.clean_lines) - 1, True, j

    def _is_control(self, stripped: str) -> bool:
        head = re.match(r"[A-Za-z_]\w*", stripped)
        return bool(head) and head.group(0) in self.CONTROL

    def _indent_of(self, i: int) -> str:
        if 0 <= i < len(self.lines):
            return re.match(r"[ \t]*", self.lines[i]).group(0)
        return ""

    def _anchor_start(self, i: int) -> int:
        """把紧邻的注解/装饰器行（@Override 等）并入符号起点"""
        j = i
        while j - 1 >= 0 and self.clean_lines[j - 1].lstrip().startswith("@"):
            j -= 1
        return j

    @staticmethod
    def _clean_sig(line: str) -> str:
        return re.sub(r"\s*\{?\s*$", "", line.strip())[:120]

    def _collect_calls(self, start: int, end: int) -> Set[str]:
        out: Set[str] = set()
        text = "\n".join(self.clean_lines[start:end + 1])
        for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", text):
            name = m.group(1)
            if name not in self.CONTROL:
                out.add(name)
        return out

    # ---------------- 解析 ----------------

    def parse(self) -> ParseResult:
        imports: List[str] = []
        symbols: List[SymbolRange] = []
        calls: Dict[str, Set[str]] = {}

        i, n = 0, len(self.clean_lines)
        while i < n:
            stripped = self.clean_lines[i].strip()
            if not stripped or self._is_control(stripped):
                i += 1
                continue
            for pat in self.IMPORT_PATTERNS:
                m = re.match(pat, self.lines[i].strip())
                if m:
                    imports.append(m.group(1))
                    break
            end, has_block, brace = self._match_at(i, "", symbols, calls)
            if end is None:
                i += 1
                continue
            i = end + 1 if has_block else i + 1
        return ParseResult(self.LANG, len(self.lines), imports, [], symbols, calls)

    def _match_at(self, i: int, prefix: str, symbols: List[SymbolRange],
                  calls: Dict[str, Set[str]], outer_end: Optional[int] = None):
        """尝试把第 i 行解析为类型/函数符号；成功则登记并返回 (结束行, 是否有块)"""
        stripped = self.clean_lines[i].strip()
        hit = None
        for pat, kind in self.TYPE_PATTERNS:
            m = re.match(pat, stripped)
            if m:
                hit = (m, kind)
                break
        if hit is None:
            pats = (self.MEMBER_PATTERNS or self.FUNC_PATTERNS) if prefix else self.FUNC_PATTERNS
            for pat in pats:
                m = re.match(pat, stripped)
                if m:
                    hit = (m, "method" if prefix else "function")
                    break
        if hit is None:
            return None, False, None

        m, kind = hit
        end, has_block, brace = self._scan(i)
        if outer_end is not None and end > outer_end:
            return None, False, None
        start = self._anchor_start(i)
        raw_name = m.group("name")
        name = f"{prefix}.{raw_name}" if prefix else raw_name
        if has_block and brace == end:      # 单行块：体与符号同行
            body_start = body_end = i + 1
        elif has_block:
            body_start, body_end = brace + 2, end
        else:
            body_start = body_end = end + 1
        symbols.append(SymbolRange(
            name=name, kind=kind, start=start + 1, end=end + 1,
            signature=self._signature(raw_name, prefix, kind, stripped),
            indent=self._indent_of(start), body_start=body_start, body_end=body_end))
        if kind in ("function", "method"):
            cs = self._collect_calls(i, end)
            cs -= {name, raw_name, raw_name.split("::")[-1]}
            calls[name] = cs
        if has_block and kind not in ("function", "method"):
            self._parse_members(i, end, name, symbols, calls)
        return end, has_block, brace

    def _signature(self, raw_name: str, prefix: str, kind: str, stripped: str) -> str:
        sig = self._clean_sig(stripped)
        if not prefix:
            return sig
        if kind not in ("function", "method"):
            return f"{prefix}.{raw_name}"
        pm = re.search(r"\(([^)]*)\)", sig)
        args = pm.group(1).strip() if pm else ""
        return f"{prefix}.{raw_name}({args})"

    def _parse_members(self, start: int, end: int, cls_name: str,
                       symbols: List[SymbolRange], calls: Dict[str, Set[str]]) -> None:
        """在类型体内扫一遍，把成员登记为 ClassName.member（跳过嵌套块）"""
        i = start + 1
        while i < end:
            stripped = self.clean_lines[i].strip()
            if not stripped or self._is_control(stripped):
                i += 1
                continue
            m_end, has_block, _ = self._match_at(i, cls_name, symbols, calls, outer_end=end)
            if m_end is None:
                i += 1
                continue
            i = m_end + 1 if has_block else i + 1


class JavaParser(BraceParser):
    LANG = "java"
    IMPORT_PATTERNS = [r"^import\s+(?:static\s+)?([\w\.\*]+)\s*;"]
    TYPE_PATTERNS = [
        (r"^(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
         r"(?:class|interface|enum|record|@interface)\s+(?P<name>\w+)", "class"),
    ]
    FUNC_PATTERNS = [
        r"^(?:(?:public|private|protected|static|final|synchronized|abstract|native|default|\s)*"
         r"[\w<>\[\],\?\.\s]+?)\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*\(",
        r"^(?:(?:public|private|protected)\s+)*(?P<name>[A-Z]\w*)\s*\(",   # 构造器
    ]


class KotlinParser(BraceParser):
    LANG = "kotlin"
    IMPORT_PATTERNS = [r"^import\s+([\w\.\*]+)"]
    TYPE_PATTERNS = [
        (r"^(?:(?:public|private|internal|protected|open|abstract|sealed|data|inner|value|"
         r"annotation|enum|actual|expect|companion)\s+)*"
         r"(?:class|interface|object)\s+(?P<name>\w+)", "class"),
    ]
    FUNC_PATTERNS = [
        r"^(?:(?:public|private|internal|protected|open|override|suspend|inline|operator|infix|"
         r"tailrec|external|actual|expect|abstract)\s+)*"
        r"fun\s+(?:<[^>]*>\s*)?(?:[\w\.]+\.)?(?P<name>\w+)\s*\(",
    ]


class CppParser(BraceParser):
    LANG = "cpp"
    IMPORT_PATTERNS = [r"^[ \t]*#[ \t]*include\s*[<\"]([^>\"]+)[>\"]"]
    TYPE_PATTERNS = [
        (r"^(?:template\s*<[^>]*>\s*)?(?:class|struct|union)\s+(?P<name>\w+)(?!\s*;)", "class"),
    ]
    FUNC_PATTERNS = [
        r"^(?:template\s*<[^>]*>\s*)?(?:[\w:<>,\*&\s~]+?)\s+(?P<name>~?\w+(?:::\w+)*)\s*\(",
    ]
    MEMBER_PATTERNS = FUNC_PATTERNS + [
        r"^(?:(?:explicit|constexpr|virtual|inline)\s+)*(?P<name>~?[A-Z]\w*)\s*\(",   # 构造/析构
    ]


class CSharpParser(BraceParser):
    LANG = "csharp"
    IMPORT_PATTERNS = [r"^using\s+([\w\.]+)\s*;"]
    TYPE_PATTERNS = [
        (r"^(?:(?:public|private|protected|internal|abstract|sealed|static|partial|readonly|"
         r"unsafe|ref|file|scoped)\s+)*"
         r"(?:class|interface|struct|enum|record|namespace)\s+(?P<name>[\w\.]+)", "class"),
    ]
    FUNC_PATTERNS = [
        r"^(?:(?:public|private|protected|internal|static|virtual|override|abstract|async|sealed|"
         r"partial|extern|unsafe|readonly|new|const|\s)*"
         r"[\w<>\[\],\?\.\s]+?)\s+(?P<name>\w+)\s*(?:<[^>]*>)?\s*\(",
        r"^(?:(?:public|private|protected|internal|static)\s+)*(?P<name>[A-Z]\w*)\s*\(",   # 构造器
    ]
    CONTROL = BraceParser.CONTROL - {"namespace", "using", "readonly"}


class JavaScriptParser(BraceParser):
    LANG = "javascript"
    IMPORT_PATTERNS = [
        r"^import\s+.*?from\s+['\"]([^'\"]+)['\"]",
        r"^import\s+['\"]([^'\"]+)['\"]",
        r"^(?:const|let|var)\s+\w+\s*=\s*require\(['\"]([^'\"]+)['\"]\)",
    ]
    TYPE_PATTERNS = [
        (r"^(?:export\s+)?(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?"
         r"(?:class|interface)\s+(?P<name>\w+)", "class"),
        (r"^(?:export\s+)?(?:declare\s+)?(?:const\s+)?enum\s+(?P<name>\w+)", "enum"),
        (r"^(?:export\s+)?(?:declare\s+)?type\s+(?P<name>\w+)\s*[=<]", "type"),
    ]
    FUNC_PATTERNS = [
        r"^(?:export\s+)?(?:declare\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>\w+)\s*\(",
        r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?:async\s*)?"
        r"(?:\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>",
        r"^(?:export\s+)?(?:const|let|var)\s+(?P<name>\w+)\s*(?::[^=]+)?=\s*(?:async\s+)?function",
    ]
    MEMBER_PATTERNS = FUNC_PATTERNS + [
        r"^(?:(?:public|private|protected|static|readonly|async|override|abstract|declare|"
         r"get|set|\*)\s+)*(?P<name>\w+)\s*(?:<[^>]*>)?\s*\(",
    ]
    CONTROL = BraceParser.CONTROL - {"readonly"}


class TypeScriptParser(JavaScriptParser):
    LANG = "typescript"


class ShellParser(BraceParser):
    LANG = "shell"
    IMPORT_PATTERNS = [
        r"^source\s+(\S+)",
        r"^\.\s+(\S+)",
    ]
    TYPE_PATTERNS: List[Tuple[str, str]] = []
    FUNC_PATTERNS = [
        r"^(?:function\s+)?(?P<name>[A-Za-z_]\w*)\s*\(\s*\)",
        r"^function\s+(?P<name>[A-Za-z_]\w*)",
    ]
    CONTROL = {"if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
               "case", "esac", "return", "echo", "export", "source", "local",
               "declare", "readonly", "set", "shift", "exit", "eval", "exec", "trap", "cd"}

    def _collect_calls(self, start: int, end: int) -> Set[str]:
        """只取每条命令的首个词（管道/&&/; 分段），过滤关键字、赋值与变量"""
        out: Set[str] = set()
        for i in range(start, min(end + 1, len(self.lines))):
            line = self.lines[i].strip()
            if not line or line.startswith("#"):
                continue
            for seg in re.split(r"\|\||&&|[|;]", line):
                seg = seg.strip()
                if not seg:
                    continue
                while re.match(r"^[A-Za-z_]\w*=\S*\s", seg + " "):
                    seg = seg.split(None, 1)[1] if " " in seg else ""
                tok = seg.split(None, 1)[0] if seg else ""
                tok = tok.strip('"\'`$(){}')
                if re.fullmatch(r"[A-Za-z_][\w\.\-]*", tok) and tok not in self.CONTROL:
                    out.add(tok)
        return out


PARSERS = {
    "python": PythonParser, "py": PythonParser,
    "java": JavaParser,
    "kotlin": KotlinParser, "kt": KotlinParser, "kts": KotlinParser,
    "c": CppParser, "cpp": CppParser, "cc": CppParser, "cxx": CppParser,
    "h": CppParser, "hpp": CppParser,
    "csharp": CSharpParser, "cs": CSharpParser,
    "javascript": JavaScriptParser, "js": JavaScriptParser, "jsx": JavaScriptParser,
    "mjs": JavaScriptParser, "cjs": JavaScriptParser,
    "typescript": TypeScriptParser, "ts": TypeScriptParser, "tsx": TypeScriptParser,
    "shell": ShellParser, "sh": ShellParser, "bash": ShellParser, "zsh": ShellParser,
}

_LANG_ALIAS = {
    "py": "python", "kt": "kotlin", "kts": "kotlin", "cc": "cpp", "cxx": "cpp",
    "h": "c", "hpp": "cpp", "cs": "csharp", "js": "javascript", "jsx": "javascript",
    "mjs": "javascript", "cjs": "javascript", "ts": "typescript", "tsx": "typescript",
    "sh": "shell", "bash": "shell", "zsh": "shell",
}


def _normalize_language(language: str) -> str:
    key = (language or "").strip().lower().lstrip(".")
    return _LANG_ALIAS.get(key, key)


def get_parser(code: str, language: str):
    key = _normalize_language(language)
    cls = PARSERS.get(key) or PARSERS.get(language or "")
    if not cls:
        supported = ", ".join(sorted({_LANG_ALIAS.get(k, k) for k in PARSERS} - set(_LANG_ALIAS)))
        raise ValueError(f"不支持的语言 {language}（可用: {supported}）")
    return cls(code)


MAX_SYMBOLS = 200


def render_skeleton(parsed: ParseResult, limit: int = MAX_SYMBOLS, with_calls: bool = False,
                    start_line: Optional[int] = None):
    """渲染紧凑骨架，返回 (text, truncated, next_start_line)

    每个符号一行：`签名  L起-止`；成员缩进两格。超 limit 行截断并给出续看行号。
    """
    syms = parsed.symbols
    if start_line:
        syms = [s for s in syms if s.end >= start_line]
    head = [f"{parsed.language} | {parsed.lines} lines"]
    if parsed.imports:
        head.append(f"imports: {', '.join(parsed.imports[:40])}")
    if parsed.globals:
        head.append(f"globals: {', '.join(parsed.globals[:40])}")
    out: List[str] = list(head)
    truncated = False
    next_start = None
    for idx, s in enumerate(syms):
        if len(out) - len(head) >= limit:
            truncated = True
            next_start = s.start
            break
        indent = "  " if s.kind == "method" else ""
        out.append(f"{indent}{s.signature}  L{s.start}-{s.end}")
    if with_calls and parsed.calls:
        rows = [f"  {k} -> {', '.join(sorted(v)[:12])}" for k, v in parsed.calls.items() if v]
        if rows:
            out.append("calls:")
            out += rows[:limit]
            if len(rows) > limit:
                truncated = True
    return "\n".join(out), truncated, next_start


def build_skeleton(code: str, language: str, limit: int = MAX_SYMBOLS, with_calls: bool = False,
                   start_line: Optional[int] = None):
    """解析 code 并返回骨架（解析失败抛异常，由调用方转错误信封）"""
    parsed = get_parser(code, language).parse()
    parsed.language = _normalize_language(language) or parsed.language
    return render_skeleton(parsed, limit, with_calls, start_line)


# ============================ 文件 / URL 读取 ============================

_EXT_LANG = {
    ".py": "python", ".pyw": "python",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
}


def infer_language(file_path: str) -> Optional[str]:
    """按扩展名推断语言（用于 file_path 模式自动补齐 language）"""
    return _EXT_LANG.get(os.path.splitext(file_path or "")[1].lower())


def _looks_binary(raw: bytes) -> bool:
    chunk = raw[:8192]
    if b"\x00" in chunk:
        return True
    text_chars = bytearray({7, 8, 9, 10, 11, 12, 13, 27, 0x81, 0x8d, 0x8f, 0x90, 0x9d})
    odd = sum(1 for b in chunk if b < 32 and b not in text_chars)
    return bool(chunk) and odd / float(len(chunk)) > 0.30


def _decode_bytes(raw: bytes):
    """自动探测编码并解码，返回 (text, encoding)。latin-1 兜底不会失败。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030", "big5"):
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1"), "latin-1"


def _detect_newline(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def read_file(file_path: str):
    """读取本地文件，返回 (text, encoding, newline, err)。

    text 统一为 '\\n' 换行，newline 记录原文件换行风格（写入时还原）。
    """
    if os.path.isdir(file_path):
        return None, None, None, f"路径是目录: {file_path}"
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None, None, None, f"文件不存在: {file_path}"
    except OSError as e:
        return None, None, None, f"读取失败 - {e}"
    if _looks_binary(raw):
        return None, None, None, f"二进制文件不支持按文本处理: {file_path}（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw)
    newline = _detect_newline(text)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, newline, None


def _symlink_guard(file_path):
    """软链接保护：写入软链接会用普通文件顶掉链接节点（os.replace 语义），故直接拒绝。

    返回错误文案；不是软链接（或无法判断）返回 None。真实路径一并给出，便于调用方改道。
    """
    if not file_path:
        return None
    try:
        if not os.path.islink(file_path):
            return None
        real = os.path.realpath(file_path)
    except OSError:
        return None
    return (f"{file_path} 是软链接，拒绝写入（继续写会把链接本身替换成普通文件）；"
            f"请改用真实路径：{real}")


def write_file(file_path: str, text: str, encoding: str = "utf-8", newline: str = "\n"):
    """原子写回文件（先编码校验，再写临时文件并 os.replace）；返回错误信息或 None。

    目标是软链接时直接拒绝，理由见 _symlink_guard；写回会保留原文件权限位，
    避免 Linux 下编辑脚本丢掉可执行位（0755 -> 0644）。
    """
    link_err = _symlink_guard(file_path)
    if link_err:
        return link_err
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    data = normalized if newline in (None, "", "\n") else normalized.replace("\n", newline)
    enc = encoding or "utf-8"
    try:
        payload = data.encode(enc)
    except (UnicodeEncodeError, LookupError) as e:
        return f"内容无法用原编码 {enc} 表示，已放弃写入 - {e}"
    mode = None
    try:
        mode = os.stat(file_path).st_mode & 0o7777
    except OSError:
        mode = None                      # 新建文件：沿用系统默认权限
    tmp = f"{file_path}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as f:
            f.write(payload)
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                pass
        os.replace(tmp, file_path)
    except OSError as e:
        try:
            os.chmod(tmp, 0o666)         # 临时文件可能已套上只读权限，先放开再删
        except OSError:
            pass
        try:
            os.remove(tmp)
        except OSError:
            pass
        return f"写入失败 - {e}"
    return None


def fetch_url(url: str, timeout: int = 30):
    """下载 http(s) 文本，返回 (text, encoding, err)；自动探测编码，统一 '\\n'。"""
    try:
        with urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as e:
        return None, None, f"下载失败 - {e}"
    if _looks_binary(raw):
        return None, None, f"二进制内容不支持按文本处理（{len(raw)} bytes）"
    text, enc = _decode_bytes(raw)
    return text.replace("\r\n", "\n").replace("\r", "\n"), enc, None


# ============================ 通用输出辅助 ============================

DEFAULT_LIMIT = 400
DIFF_LIMIT = 120          # 超过此行数的 diff 折叠为 hunk 索引
DIFF_HUNKS = 20           # 折叠模式下最多列出的 hunk 数
MAX_CANDIDATES = 30


def _is_plain(language) -> bool:
    """语言为空或不在解析器表中时按纯文本处理（md/txt/json 等）"""
    key = _normalize_language(language)
    return not key or key not in PARSERS


def _short_path(path: str) -> str:
    return os.path.basename(path or "") or (path or "")


def _num_width(total: int) -> int:
    return max(2, len(str(max(total, 1))))


def _numbered(lines: List[str], start: int, end: int) -> str:
    """按绝对行号渲染 [start, end]（1-based，含端点）"""
    w = _num_width(end)
    return "\n".join(f"{i:>{w}}: {lines[i - 1]}" for i in range(start, end + 1)
                     if 1 <= i <= len(lines))


def _auto_indent(new_code: str, indent: str) -> str:
    """new_code 首行无缩进且目标上下文有缩进时，整体补齐缩进"""
    if not indent or not new_code:
        return new_code
    rows = new_code.split("\n")
    first = next((r for r in rows if r.strip()), "")
    if first[:1] in (" ", "\t"):
        return new_code
    return "\n".join(indent + r if r.strip() else r for r in rows)


# 定义/声明类语句的首行特征：命中说明新内容是一个新的「块」，插入时宜与上文空行分隔
_DEF_LIKE_RE = re.compile(
    r"^(?:@|async\s+def\b|def\b|class\b|export\s|function\b|interface\b|enum\b|struct\b|impl\b|"
    r"fn\b|func\b|namespace\b|type\b|public\b|private\b|protected\b|static\b|abstract\b|"
    r"sub\b|[A-Za-z_]\w*\s*\(\s*\)\s*\{)")


def _def_like(text: str) -> bool:
    """new_code 的首个非空行是否是定义/声明类语句"""
    for line in (text or "").split("\n"):
        s = line.strip()
        if s:
            return bool(_DEF_LIKE_RE.match(s))
    return False


def _pad_blank(lines: List[str], at: int, text: str, next_is_def: bool = False) -> str:
    """按需给插入内容补空行；`at` 语义同 _insert_at_line（插到第 at 行之后，0 = 文件开头）。

    - 新内容以 def/class/function 等定义开头、且插入点上一行不是空行 -> 前面补 1 个空行；
    - next_is_def=true（后面紧挨着一个定义）-> 末尾补到恰有 1 个空行；
    只调整插入内容自身，不改动文件其它部分。
    """
    if not text.strip():
        return text
    prev = lines[at - 1] if 1 <= at <= len(lines) else ""
    if _def_like(text) and prev.strip() and not text.startswith("\n"):
        text = "\n" + text
    if next_is_def and not text.endswith("\n\n"):
        text = text.rstrip("\n") + "\n\n"
    return text


def _body_indent(lines: List[str], sym: SymbolRange) -> str:
    """取符号体的缩进（首个非空体行）；无体则退化为符号缩进 + 4 空格"""
    for i in range(sym.body_start, min(sym.body_end, len(lines)) + 1):
        if 1 <= i <= len(lines) and lines[i - 1].strip():
            return re.match(r"[ \t]*", lines[i - 1]).group(0)
    if sym.body_start == sym.start and sym.body_end == sym.end:
        return sym.indent + "    " if sym.kind in ("class", "function", "method") else sym.indent
    return sym.indent


def _closest_lines(code: str, old_code: str, top: int = 3):
    """old_code 未命中时，给出最接近的行窗口（带行号），便于二次精确命中"""
    lines = code.splitlines()
    want = old_code.splitlines()
    if not want or not lines:
        return []
    n = len(want)
    first = want[0].strip()
    idxs = set()
    for hit in difflib.get_close_matches(first, [l.strip() for l in lines], n=8, cutoff=0.5):
        for i, l in enumerate(lines):
            if l.strip() == hit:
                idxs.add(i)
    if not idxs:
        idxs = set(range(min(len(lines), 200)))
    scored = []
    for i in sorted(idxs):
        window = "\n".join(lines[i:i + n])
        r = difflib.SequenceMatcher(None, old_code, window).ratio()
        scored.append((r, i + 1, min(i + n, len(lines)), window))
    scored.sort(key=lambda x: -x[0])
    out = []
    for r, s, e, window in scored[:top]:
        if r < 0.4:
            continue
        text = window if len(window) <= 300 else window[:300] + "…"
        out.append({"lines": str(s) if s == e else f"{s}-{e}", "ratio": round(r, 2), "text": text})
    return out


# ============================ read：符号 / 正则 / 行范围 / 骨架 ============================

def _resolve_symbols(parsed: ParseResult, symbol, pattern):
    """按符号名（支持裸方法名）与正则筛选，返回 (selected, err_payload)"""
    names = parsed.symbols
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return None, error(f"正则表达式非法: {e}", code="bad_pattern")

    selected: List[SymbolRange] = []
    seen = set()
    for s in names:
        if rx and rx.search(s.name):
            if s.name not in seen:
                selected.append(s)
                seen.add(s.name)
    for w in wanted:
        exact = [s for s in names if s.name == w]
        if not exact:
            exact = [s for s in names
                     if s.name.replace("::", ".").split(".")[-1] == w or s.name.endswith("." + w)]
        if not exact:
            close = difflib.get_close_matches(w, [s.name for s in names], n=5, cutoff=0.6)
            avail = _symbol_list(names)
            hint = f"，是否想找: {', '.join(close)}" if close else ""
            return None, error(f"未匹配到符号 '{w}'{hint}（可用: {avail}）",
                               code="symbol_not_found")
        if len(exact) > 1 and w not in [s.name for s in names]:
            cands = ", ".join(f"{s.name}(L{s.start})" for s in exact[:10])
            return None, error(f"符号 '{w}' 有 {len(exact)} 个同名成员: {cands}；请用 类名.{w} 指定",
                               code="ambiguous_symbol")
        for s in exact:
            if s.name not in seen:
                selected.append(s)
                seen.add(s.name)
    return selected, None


def _symbol_list(names: List[SymbolRange]) -> str:
    shown = [s.name for s in names[:MAX_CANDIDATES]]
    tail = f" …共 {len(names)} 个" if len(names) > MAX_CANDIDATES else ""
    return ", ".join(shown) + tail


def _emit_symbols(code: str, selected: List[SymbolRange], limit: int, src: dict):
    """输出符号体（带绝对行号），超限截断并给出可直接复用的 next 参数"""
    lines = code.splitlines()
    out: List[str] = []
    shown = 0
    truncated = False
    nxt = None
    for s in selected:
        if shown >= limit:
            truncated = True
            nxt = {"symbol": s.name}
            break
        out.append(f"# {s.name} ({s.kind}) L{s.start}-{s.end}")
        shown += 1
        remain = max(limit - shown, 1)       # 至少留 1 行，否则 next 会指回同一符号
        span = s.end - s.start + 1
        if span > remain:
            out.append(_numbered(lines, s.start, s.start + remain - 1))
            truncated = True
            nxt = {"start_line": s.start + remain, "end_line": s.end}
            break
        out.append(_numbered(lines, s.start, s.end))
        shown += span
    payload = {"action": "read", "mode": "symbol", "result": "\n".join(out)}
    if truncated:
        payload["truncated"] = True
        payload["next"] = nxt
    payload.update(src)
    return ok(payload)


def _emit_matches(code: str, symbol, pattern, limit: int, src: dict, note: str = None):
    """按 symbol（子串）/ pattern（正则）逐行匹配，带绝对行号输出"""
    lines = code.splitlines()
    rx = None
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return error(f"正则表达式非法: {e}", code="bad_pattern")
    wanted: List[str] = []
    if symbol:
        wanted = [symbol] if isinstance(symbol, str) else [str(x) for x in symbol]
    idxs = [i for i, line in enumerate(lines)
            if (rx and rx.search(line)) or any(w in line for w in wanted)]
    if not idxs:
        return error("未匹配到内容", code="not_found")
    shown = idxs[:limit]
    w = _num_width(len(lines))
    body = "\n".join(f"{i + 1:>{w}}: {lines[i]}" for i in shown)
    payload = {"action": "read", "mode": "text", "matches": len(idxs), "result": body}
    if note:
        payload["note"] = note
    if len(idxs) > limit:
        payload["truncated"] = True
        payload["next"] = f"共 {len(idxs)} 处，已显示前 {limit} 处；可加 pattern 收窄"
    payload.update(src)
    return ok(payload)


def _emit_range(code: str, start: int, end: int, limit: int, src: dict, note: str = None):
    """输出行范围（带绝对行号），超限截断并给出 next 续读参数"""
    lines = code.splitlines()
    total = len(lines)
    if start < 1 or end < 1:
        return error(f"行号从 1 开始: {start}-{end}", code="bad_range")
    if start > total:
        return error(f"起始行 {start} 超出总行数 {total}", code="bad_range")
    if start > end:
        return error(f"起始行 {start} 大于结束行 {end}", code="bad_range")
    end = min(end, total)
    truncated = False
    if end - start + 1 > limit:
        end = start + limit - 1
        truncated = True
    payload = {"action": "read", "mode": "lines", "range": f"{start}-{end}", "total": total,
               "result": _numbered(lines, start, end)}
    if note:
        payload["note"] = note
    if truncated:
        payload["truncated"] = True
        payload["next"] = {"start_line": end + 1, "end_line": total}
    payload.update(src)
    return ok(payload)


def _parse_or_fail(code: str, language: str):
    try:
        parsed = get_parser(code, language).parse()
        parsed.language = _normalize_language(language) or parsed.language
        return parsed, None
    except SyntaxError as e:
        return None, error(f"解析失败 - {e}", code="parse_failed")
    except ValueError as e:
        return None, error(str(e), code="unsupported_language")
    except Exception as e:
        return None, error(f"解析失败 - {e}", code="parse_failed")


def _read(code, language, symbol, pattern, start_line, end_line, limit, src, with_calls=False,
          plain_note=None):
    """read：优先 symbol/pattern，其次行范围，最后回退骨架（代码）或全文（纯文本）

    结构解析失败（语法错误的文件很常见）不中断，一律降级为文本检索/文本返回。
    """
    if not code.strip():
        return error("文件内容为空", code="empty_code")

    plain = _is_plain(language)
    total = len(code.splitlines())

    if symbol or pattern:
        if not plain:
            parsed, _ = _parse_or_fail(code, language)
            if parsed is not None:
                selected, serr = _resolve_symbols(parsed, symbol, pattern)
                if selected:
                    return _emit_symbols(code, selected, limit, src)
                if pattern and not symbol:      # 符号名未命中 → 回退按行 grep
                    return _emit_matches(code, None, pattern, limit, src,
                                         note="pattern 未匹配到符号名，已按行检索")
                if serr:
                    return serr
            else:                               # 解析失败 → 降级文本检索
                return _emit_matches(code, symbol, pattern, limit, src,
                                     note="结构解析失败，已按文本检索")
        return _emit_matches(code, symbol, pattern, limit, src, note=plain_note)

    if start_line is not None or end_line is not None:
        try:
            start = int(start_line) if start_line is not None else 1
            end = int(end_line) if end_line is not None else total
        except (TypeError, ValueError):
            return error("start_line / end_line 必须为整数", code="bad_range")
        return _emit_range(code, start, end, limit, src)

    if plain:
        return _emit_range(code, 1, total, limit, src, note=plain_note)
    try:
        text, truncated, next_start = build_skeleton(code, language, limit, with_calls)
    except Exception as e:
        return _emit_range(code, 1, total, limit, src, note=f"结构解析失败（{e}），已按纯文本返回")
    payload = {"action": "read", "mode": "outline", "result": text}
    if truncated:
        payload["truncated"] = True
        payload["next"] = {"action": "outline", "start_line": next_start}
    payload.update(src)
    return ok(payload)


# ============================ 写入：定位（symbol / 行号 / 锚点） ============================

def _replace(code: str, old_code: str, new_code: str, replace_all: bool):
    """精确替换，返回 (new_src, count, err, err_code)。多处匹配未开 replace_all 时报错并列行号。"""
    count = code.count(old_code)
    if count == 0:
        return None, 0, "old_code 未找到（需与源码逐字符一致，含缩进与换行）", "not_found"
    if count > 1 and not replace_all:
        return None, count, _multi_msg(code, old_code, count), "multiple_matches"
    if replace_all:
        return code.replace(old_code, new_code), count, None, None
    return code.replace(old_code, new_code, 1), 1, None, None


def _multi_msg(code: str, old_code: str, count: int) -> str:
    hits: List[int] = []
    start = 0
    for _ in range(count):
        idx = code.find(old_code, start)
        hits.append(code.count("\n", 0, idx) + 1)
        start = idx + len(old_code)
    return (f"old_code 匹配到 {count} 处（行 {', '.join(map(str, hits[:20]))}），无法确定目标；"
            f"请加长片段，或设 replace_all=true 全部替换")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _split_lines(code: str):
    trailing = code.endswith("\n")
    lines = code.split("\n")
    if trailing and lines and lines[-1] == "":
        lines.pop()
    return lines, trailing


def _join_lines(lines: List[str], trailing: bool) -> str:
    text = "\n".join(lines)
    if trailing and text:
        text += "\n"
    return text


def _new_lines(new_code: str) -> List[str]:
    if not new_code:
        return []
    lines = new_code.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _replace_lines(code: str, start, end, new_code: str):
    """按行号替换 [start, end]（1-based，含端点）为 new_code"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    if start is None or start < 1 or start > total:
        return None, f"起始行需在 1-{total}: {start}"
    if end is None:
        end = start
    if end < start or end > total:
        return None, f"结束行需在 {start}-{total}: {end}"
    lines[start - 1:end] = _new_lines(new_code)
    return _join_lines(lines, trailing), None


def _insert_at_line(code: str, at: int, new_code: str):
    """在第 at 行之后插入 new_code（0 = 文件开头）"""
    lines, trailing = _split_lines(code)
    total = len(lines)
    if at < 0 or at > total:
        return None, f"插入位置需在 0-{total}: {at}"
    lines[at:at] = _new_lines(new_code)
    return _join_lines(lines, max(trailing, bool(_new_lines(new_code)))), None


def _add_after(code: str, anchor: str, new_code: str, replace_all: bool, before: bool = False):
    """在 anchor 之后（或之前）按行边界插入 new_code，返回 (new_src, count, err, err_code)"""
    count = code.count(anchor)
    if count == 0:
        return None, 0, "old_code 未找到（需与源码逐字符一致，含缩进与换行）", "not_found"
    if count > 1 and not replace_all:
        return None, count, _multi_msg(code, anchor, count), "multiple_matches"

    def seg(prev_char: str, next_char: str) -> str:
        out = ""
        if not before:
            if not anchor.endswith("\n") and not new_code.startswith("\n"):
                out += "\n"
            out += new_code
            if not new_code.endswith("\n") and next_char != "\n":
                out += "\n"
        else:
            if not new_code.endswith("\n") and not anchor.startswith("\n"):
                out += new_code + "\n"
            else:
                out += new_code
        return out

    out: List[str] = []
    pos = 0
    inserted = 0
    while True:
        idx = code.find(anchor, pos)
        if idx == -1:
            out.append(code[pos:])
            break
        end = idx + len(anchor)
        if before:
            out.append(code[pos:idx])
            if replace_all or inserted == 0:
                out.append(seg(code[idx - 1:idx], anchor[:1]))
                inserted += 1
            out.append(anchor)
            if not replace_all:
                out.append(code[end:])
                break
            pos = end
            continue
        out.append(code[pos:end])
        if replace_all or inserted == 0:
            out.append(seg(code[end - 1:end], code[end:end + 1]))
            inserted += 1
            if not replace_all:
                out.append(code[end:])
                break
        pos = end
    return "".join(out), count, None, None


# ============================ diff / 落盘 ============================

def _make_diff(before: str, after: str):
    """生成 unified diff（3 行上下文），返回 (text, truncated, hunks)

    小改动给完整 diff；超过 DIFF_LIMIT 行时只给 hunk 索引与增删行数，避免刷屏。
    """
    raw = list(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                    fromfile="before", tofile="after", lineterm="", n=3))
    if not raw:
        return "", False, 0
    hunks, cur = [], None
    for line in raw[2:]:
        if line.startswith("@@"):
            cur = [line]
            hunks.append(cur)
        elif cur is not None:
            cur.append(line)
    body_total = len(raw) - 2
    if body_total <= DIFF_LIMIT:
        return "\n".join(raw), False, len(hunks)

    rows = []
    for h in hunks[:DIFF_HUNKS]:
        add = sum(1 for l in h[1:] if l.startswith("+"))
        rem = sum(1 for l in h[1:] if l.startswith("-"))
        rows.append(f"{h[0]}  -{rem}/+{add}")
    text = "\n".join(rows)
    if len(hunks) > DIFF_HUNKS:
        text += f"\n... 另有 {len(hunks) - DIFF_HUNKS} 处改动"
    text += f"\n(diff 共 {body_total} 行，已折叠为 hunk 索引；可用 read + start_line 查看)"
    return text, True, len(hunks)


def _finish(file_path, encoding, newline, action, count, count_key, new_src,
            before=None, dry_run=False, extra=None):
    """落盘并返回精简摘要 + 变更 diff（不含全量源码，省 token）"""
    payload = {"action": action, "file": _short_path(file_path), count_key: count}
    if before is not None:
        payload["lines"] = [len(before.splitlines()), len(new_src.splitlines())]
        text, truncated, _ = _make_diff(before, new_src)
        payload["diff"] = text
        if truncated:
            payload["diff_truncated"] = True
    if extra:
        payload.update(extra)
    if dry_run:
        payload["dry_run"] = True
        return ok(payload)
    werr = write_file(file_path, new_src, encoding, newline)
    if werr:
        return error(werr, code="io_error")
    return ok(payload)


def _create_file(file_path, new_code, dry_run=False):
    """新建文件（file_path 必须不存在）；父目录缺失时自动创建。不回显整文件 diff（内容即入参）。"""
    if not (file_path or "").strip():
        return error("create 需要提供 file_path（不能只给 url）", code="missing_argument")
    if os.path.isdir(file_path):
        return error(f"路径是目录: {file_path}", code="is_directory")
    if os.path.exists(file_path):
        return error(f"文件已存在: {file_path}（改用 edit/add）", code="already_exists")

    text = new_code or ""
    if text and not text.endswith("\n"):
        text += "\n"
    parent = os.path.dirname(os.path.abspath(file_path))
    need_dir = bool(parent) and not os.path.isdir(parent)
    payload = {"action": "create", "file": _short_path(file_path),
               "lines": len(text.splitlines()), "bytes": len(text.encode("utf-8"))}
    if dry_run:
        payload["dry_run"] = True
        return ok(payload)
    if need_dir:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return error(f"创建目录失败 - {e}", code="io_error")
        payload["dir_created"] = parent
    werr = write_file(file_path, text, "utf-8", "\n")
    if werr:
        return error(werr, code="io_error")
    return ok(payload)


# ============================ 入口 ============================

def _norm_text(value):
    if value is None:
        return None
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def script_editor(action: str = None, file_path: str = None, url: str = None,
                  language: str = None, symbol=None, pattern: str = None,
                  start_line: int = None, end_line: int = None, limit: int = DEFAULT_LIMIT,
                  old_code: str = None, new_code: str = None, replace_all: bool = False,
                  dry_run: bool = False, position: str = None, with_calls: bool = False) -> str:
    """脚本编辑 + 结构化读取工具（对齐 OpenViking / DSH 规范信封）。

    数据来源（二选一）：file_path（本地文件，可写）或 url（http/https，只读）。

    动作：
    - read    : symbol（单个/数组，支持裸方法名）/ pattern（正则，先符号名后按行）/
                start_line~end_line 行范围；都不给则返回结构骨架
    - outline : 结构骨架（含每个符号起止行号；with_calls=true 才附调用关系）
    - create  : 新建文件（file_path 必须不存在；父目录缺失时自动创建）
    - add     : 插入 new_code，定位优先级 symbol > start_line > old_code > 文件末尾
    - edit    : 替换，定位优先级 symbol > start_line > old_code
    - delete  : 删除，定位优先级 symbol > start_line > old_code
    position（配合 symbol）：start | end | before | after（add 默认 end，其余默认整体）
    写入动作默认回显 unified diff；dry_run=true 只预览不落盘。
    """
    act = (action or "").strip().lower()
    old_code = _norm_text(old_code)
    new_code = _norm_text(new_code) or ""
    pos = (position or "").strip().lower()
    try:
        limit = int(limit) if limit else DEFAULT_LIMIT
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    if limit < 1:
        limit = DEFAULT_LIMIT

    src = {}
    if file_path:
        src["file"] = _short_path(file_path)
    elif url:
        src["url"] = url
    else:
        return error("需要提供 file_path 或 url", code="missing_source")

    if act not in ("read", "outline", "add", "edit", "delete", "create"):
        return error(f"未知操作 '{action}'（应为 read/outline/add/edit/delete/create）",
                     code="unknown_action")

    # 写动作（含 create）一律拒绝软链接：os.replace 会用普通文件顶掉链接节点，
    # 把链接关系毁掉；只读动作不受影响
    if act in ("add", "edit", "delete", "create"):
        link_err = _symlink_guard(file_path)
        if link_err:
            return error(link_err, code="is_symlink")

    if act == "create":
        return _create_file(file_path, new_code, dry_run)

    encoding, newline = "utf-8", "\n"
    writable = False
    if file_path:
        code, encoding, newline, io_err = read_file(file_path)
        if io_err:
            code_err = ("is_directory" if "目录" in io_err else
                        "binary_file" if "二进制" in io_err else "io_error")
            return error(io_err, code=code_err)
        writable = True
        if not language:
            language = infer_language(file_path)
    else:
        code, encoding, io_err = fetch_url(url)
        if io_err:
            return error(io_err, code="io_error")
        if not language:
            language = infer_language(urlparse(url).path)
    if code is None:
        code = ""

    # 显式传了不支持结构解析的语言时给一句提示（.md/.json 等不传 language，不受影响）
    lang_note = (f"language={language} 无结构解析，已按纯文本处理"
                 if language and _is_plain(language) else None)

    if act == "read":
        return _read(code, language, symbol, pattern, start_line, end_line, limit, src,
                     with_calls, lang_note)

    if act == "outline":
        if not code.strip():
            return error("文件内容为空", code="empty_code")
        if _is_plain(language):
            text, truncated, _ = render_skeleton(
                ParseResult("text", len(code.splitlines()), [], [], [], {}), limit)
            payload = {"action": "outline", "result": text}
            if lang_note:
                payload["note"] = lang_note
            if truncated:
                payload["truncated"] = True
            payload.update(src)
            return ok(payload)
        try:
            text, truncated, next_start = build_skeleton(code, language, limit, with_calls,
                                                        _as_int(start_line))
        except SyntaxError as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        except ValueError as e:
            return error(str(e), code="unsupported_language")
        except Exception as e:
            return error(f"解析失败 - {e}", code="parse_failed")
        payload = {"action": "outline", "language": _normalize_language(language), "result": text}
        if truncated:
            payload["truncated"] = True
            payload["next"] = {"action": "outline", "start_line": next_start}
        payload.update(src)
        return ok(payload)

    # add / edit / delete 需要可写来源
    if not writable:
        return error("url 为只读，不能修改", code="read_only")

    lines = code.splitlines()
    sym = None
    use_symbol = bool(symbol)
    line_mode = start_line is not None

    if use_symbol and (line_mode or old_code):
        return error("symbol 与 start_line/old_code 互斥，请只给一种定位方式",
                     code="conflicting_target")
    if line_mode and old_code and act in ("edit", "delete"):
        s, e = _as_int(start_line), _as_int(end_line)
        if s and e and s >= 1 and e <= len(lines) and s <= e:
            actual = "\n".join(lines[s - 1:e])
            if actual.rstrip("\n") != old_code.rstrip("\n") and actual.strip() != old_code.strip():
                return error(
                    f"start_line~end_line ({s}-{e}) 的内容与 old_code 不一致，已中止以免改错行；"
                    f"实际内容为:\n{_numbered(lines, s, min(e, s + 20))}",
                    code="range_mismatch")
        else:
            return error(f"行范围非法: {s}-{e}（共 {len(lines)} 行）", code="bad_range")

    if use_symbol:
        if _is_plain(language):
            return error("纯文本文件无符号结构，请用 start_line 或 old_code 定位",
                         code="unsupported_language")
        parsed, perr = _parse_or_fail(code, language)
        if perr:
            return perr
        selected, serr = _resolve_symbols(parsed, symbol, None)
        if serr:
            return serr
        if len(selected) != 1:
            cands = ", ".join(f"{s.name}(L{s.start}-{s.end})" for s in selected[:10])
            return error(f"symbol 需唯一命中，实际 {len(selected)} 个: {cands}",
                         code="ambiguous_symbol")
        sym = selected[0]

    if act == "delete":
        if sym is not None:
            s, e = sym.start, sym.end
            # 连同紧随的空行一起删（最多 2 行）；以符号末尾为基准，否则长符号会把自身跨度
            # 算进容差，导致一个空行都不删
            while e + 1 <= len(lines) and e - sym.end < 2 and not lines[e].strip():
                e += 1
            new_src, err = _replace_lines(code, s, e, "")
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "delete", 1, "deleted", new_src,
                           before=code, dry_run=dry_run,
                           extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            new_src, err = _replace_lines(code, _as_int(start_line), _as_int(end_line), "")
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "delete", 1, "deleted", new_src,
                           before=code, dry_run=dry_run,
                           extra={"range": f"{_as_int(start_line)}-{_as_int(end_line) or _as_int(start_line)}"})
        if not old_code:
            return error("delete 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        new_src, count, err, ecode = _replace(code, old_code, "", replace_all)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return _finish(file_path, encoding, newline, "delete", count, "deleted", new_src,
                       before=code, dry_run=dry_run)

    if act == "edit":
        if sym is not None:
            s, e = sym.start, sym.end
            body = _auto_indent(new_code, sym.indent)
            if pos == "before":
                new_src, err = _insert_at_line(code, s - 1, body)
            elif pos == "after":
                new_src, err = _insert_at_line(code, e, body)
            else:
                new_src, err = _replace_lines(code, s, e, body)
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "edit", 1, "replaced", new_src,
                           before=code, dry_run=dry_run,
                           extra={"symbol": sym.name, "range": f"{s}-{e}"})
        if line_mode:
            s, e = _as_int(start_line), _as_int(end_line)
            indent = re.match(r"[ \t]*", lines[s - 1]).group(0) if 1 <= (s or 0) <= len(lines) else ""
            new_src, err = _replace_lines(code, s, e, _auto_indent(new_code, indent))
            if err:
                return error(err, code="bad_range")
            return _finish(file_path, encoding, newline, "edit", 1, "replaced", new_src,
                           before=code, dry_run=dry_run, extra={"range": f"{s}-{e or s}"})
        if not old_code:
            return error("edit 需要提供 symbol、start_line 或 old_code", code="missing_argument")
        if new_code == old_code:
            return error("new_code 与 old_code 相同，无需修改", code="no_change")
        new_src, count, err, ecode = _replace(code, old_code, new_code, replace_all)
        if err:
            return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                         code=ecode)
        return _finish(file_path, encoding, newline, "edit", count, "replaced", new_src,
                       before=code, dry_run=dry_run)

    # ---------------- add ----------------
    if not new_code:
        return error("add 需要提供 new_code", code="missing_argument")
    if sym is not None:
        where = pos or "end"
        if where == "end":
            at, indent = sym.body_end, _body_indent(lines, sym)
        elif where == "start":
            at, indent = sym.body_start - 1, _body_indent(lines, sym)
        elif where == "after":
            at, indent = sym.end, sym.indent
        elif where == "before":
            at, indent = sym.start - 1, sym.indent
        else:
            return error(f"position 非法 '{position}'（应为 start/end/before/after）",
                         code="invalid_argument")
        body = _auto_indent(new_code, indent)
        if where != "start":             # 插进体的首行属于语句流，不额外补空行
            next_is_def = where == "before" or (at < len(lines) and _def_like(lines[at]))
            body = _pad_blank(lines, at, body, next_is_def=next_is_def)
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run,
                       extra={"symbol": sym.name, "at_line": at, "position": where})
    if line_mode:
        at = _as_int(start_line)
        if at is None or at < 0 or at > len(lines):
            return error(f"插入位置需在 0-{len(lines)}: {start_line}", code="bad_range")
        anchor = lines[at - 1] if 1 <= at <= len(lines) else ""
        indent = re.match(r"[ \t]*", anchor).group(0) if anchor.strip() else ""
        body = _pad_blank(lines, at, _auto_indent(new_code, indent),
                          next_is_def=at < len(lines) and _def_like(lines[at]))
        new_src, err = _insert_at_line(code, at, body)
        if err:
            return error(err, code="bad_range")
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run, extra={"at_line": at})
    if not old_code:
        body = new_code
        # 追加到文件末尾的新定义：与上一行之间留一个空行
        if (_def_like(body) and lines and lines[-1].strip()
                and not code.endswith("\n\n") and not body.startswith("\n")):
            body = "\n" + body
        sep = "" if not code or code.endswith("\n") or body.startswith("\n") else "\n"
        new_src = code + sep + body
        if not new_src.endswith("\n"):
            new_src += "\n"
        return _finish(file_path, encoding, newline, "add", 1, "inserted", new_src,
                       before=code, dry_run=dry_run, extra={"at_line": len(lines) + 1})
    new_src, count, err, ecode = _add_after(code, old_code, new_code, replace_all,
                                            before=(pos == "before"))
    if err:
        return error(err + (_hint_msg(code, old_code) if ecode == "not_found" else ""),
                     code=ecode)
    return _finish(file_path, encoding, newline, "add", count, "inserted", new_src,
                   before=code, dry_run=dry_run)


def _hint_msg(code: str, old_code: str) -> str:
    """old_code 未命中时附最接近的行窗口（文本形式，兼容两端 error 信封）"""
    cands = _closest_lines(code, old_code)
    if not cands:
        return ""
    rows = "  ".join(f"L{c['lines']}({c['ratio']:.2f})「{(c['text'].splitlines() or [''])[0][:80]}」"
                     for c in cands)
    return f"；最接近 -> {rows}"
