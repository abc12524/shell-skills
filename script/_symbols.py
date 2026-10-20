"""语法层：符号结构解析（8 种语言）+ 骨架渲染。

Python 走标准库 ast；C/Java/Kotlin/C#/JS/TS/Shell 走基于正则+括号配对的 BraceParser。
对外只暴露 get_parser() / build_skeleton() / render_skeleton()，不涉及文件 IO。
"""

from dataclasses import dataclass
from dataclasses import field
import ast
import re

from typing import Dict, List, Optional, Set, Tuple


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


MAX_CANDIDATES = 30


def _is_plain(language) -> bool:
    """语言为空或不在解析器表中时按纯文本处理（md/txt/json 等）"""
    key = _normalize_language(language)
    return not key or key not in PARSERS
