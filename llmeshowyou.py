#!/usr/bin/env python3
"""
llmeshowyou -- Language-aware file-to-markdown mapper for LLM context efficiency.
Scans large source files via AST (Python) or tree-sitter (multi-language) and
produces a compact markdown reference so an LLM can navigate the file by line
range without consuming the full source. Each map embeds a SHA-256 hash for
staleness-aware incremental updates.

Also builds cross-file import graphs, god-node rankings, HTML visualizations,
wiki exports, and supports query/path/explain over the graph.

Commands:
  map     <files|globs|.> [--recursive] [--min-lines N] [--combined] [--outdir DIR]
  update  <paths...>      [--major-only] [--force] [--check]
  status  [dir]           [--recursive]
  graph   <dir>           [--combined] [--html] [--outdir DIR] [--no-cluster]
  query   "<question>"    [--dfs] [--budget N]
  path    <start> <end>
  explain <node>
  wiki    <dir>           [--outdir DIR]
  graphml <dir>           [--out]
  neo4j   <dir>           [--out]
  cost    [dir]
  hook    (install|uninstall|status) [--project]
  mcp     <dir>           [--transport stdio|http] [--port PORT]
  watch   <dir>           [--recursive] [--interval N]
"""

# #[Imports]
import ast
import argparse
import hashlib
import json
import os
import re
import sys
import glob
import time
import subprocess
import shutil
import threading
import queue
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional, Any
from xml.sax.saxutils import escape as xml_escape

# #[Stdlib Module Set]
try:
    STDLIB_MODULES = sys.stdlib_module_names
except AttributeError:
    STDLIB_MODULES = frozenset({
        'abc', 'aifc', 'argparse', 'array', 'ast', 'asynchat', 'asyncio',
        'asyncore', 'atexit', 'audioop', 'base64', 'bdb', 'binascii',
        'binhex', 'bisect', 'builtins', 'bz2', 'calendar', 'cgi', 'cgitb',
        'chunk', 'cmath', 'cmd', 'code', 'codecs', 'codeop', 'collections',
        'colorsys', 'compileall', 'concurrent', 'configparser', 'contextlib',
        'contextvars', 'copy', 'copyreg', 'cProfile', 'crypt', 'csv', 'ctypes',
        'curses', 'dataclasses', 'datetime', 'dbm', 'decimal', 'difflib',
        'dis', 'distutils', 'doctest', 'email', 'encodings', 'enum',
        'errno', 'faulthandler', 'fcntl', 'filecmp', 'fileinput',
        'fnmatch', 'fractions', 'ftplib', 'functools', 'gc', 'getopt',
        'getpass', 'gettext', 'glob', 'graphlib', 'grp', 'gzip',
        'hashlib', 'heapq', 'hmac', 'html', 'http', 'idlelib', 'imaplib',
        'imghdr', 'imp', 'importlib', 'inspect', 'io', 'ipaddress',
        'itertools', 'json', 'keyword', 'lib2to3', 'linecache', 'locale',
        'logging', 'lzma', 'mailbox', 'mailcap', 'marshal', 'math', 'mimetypes',
        'mmap', 'modulefinder', 'multiprocessing', 'netrc', 'nis', 'nntplib',
        'numbers', 'operator', 'optparse', 'os', 'ossaudiodev', 'pathlib',
        'pdb', 'pickle', 'pickletools', 'pipes', 'pkgutil', 'platform',
        'plistlib', 'poplib', 'posix', 'posixpath', 'pprint', 'profile',
        'pstats', 'pty', 'pwd', 'py_compile', 'pyclbr', 'pydoc', 'queue',
        'quopri', 'random', 're', 'readline', 'reprlib', 'resource',
        'rlcompleter', 'runpy', 'sched', 'secrets', 'select', 'selectors',
        'shelve', 'shlex', 'shutil', 'signal', 'site', 'smtpd', 'smtplib',
        'sndhdr', 'socket', 'socketserver', 'sqlite3', 'ssl', 'stat',
        'statistics', 'string', 'stringprep', 'struct', 'subprocess',
        'sunau', 'symtable', 'sys', 'sysconfig', 'syslog', 'tabnanny',
        'tarfile', 'telnetlib', 'tempfile', 'termios', 'test', 'textwrap',
        'threading', 'time', 'timeit', 'tkinter', 'token', 'tokenize',
        'tomllib', 'trace', 'traceback', 'tracemalloc', 'tty', 'turtle',
        'turtledemo', 'types', 'typing', 'unicodedata', 'unittest', 'urllib',
        'uu', 'uuid', 'venv', 'warnings', 'wave', 'weakref', 'webbrowser',
        'winreg', 'winsound', 'wsgiref', 'xdrlib', 'xml', 'xmlrpc',
        'zipapp', 'zipfile', 'zipimport', 'zlib',
    })

# #[Tree-sitter availability]
_TREESITTER_LANGS: dict[str, Any] = {}
try:
    import tree_sitter as _ts_lib
    _TS_AVAILABLE = True
except ImportError:
    _TS_AVAILABLE = False

def _init_tree_sitter() -> dict[str, Any]:
    langs: dict[str, Any] = {}
    if not _TS_AVAILABLE:
        return langs
    _LANG_MAP = {
        '.ts': 'typescript', '.tsx': 'tsx',
        '.js': 'javascript', '.jsx': 'jsx',
        '.go': 'go', '.rs': 'rust', '.java': 'java',
        '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
        '.rb': 'ruby', '.cs': 'c_sharp',
        '.kt': 'kotlin', '.swift': 'swift',
        '.php': 'php', '.scala': 'scala',
        '.lua': 'lua', '.zig': 'zig', '.dart': 'dart',
        '.jl': 'julia', '.sh': 'bash', '.bash': 'bash',
    }
    _TS_PKG = {
        'typescript': 'tree_sitter_typescript',
        'tsx': 'tree_sitter_typescript',
        'javascript': 'tree_sitter_javascript',
        'jsx': 'tree_sitter_javascript',
        'go': 'tree_sitter_go',
        'rust': 'tree_sitter_rust',
        'java': 'tree_sitter_java',
        'c': 'tree_sitter_c',
        'cpp': 'tree_sitter_cpp',
        'ruby': 'tree_sitter_ruby',
        'c_sharp': 'tree_sitter_c_sharp',
        'kotlin': 'tree_sitter_kotlin',
        'swift': 'tree_sitter_swift',
        'php': 'tree_sitter_php',
        'scala': 'tree_sitter_scala',
        'lua': 'tree_sitter_lua',
        'zig': 'tree_sitter_zig',
        'dart': 'tree_sitter_dart',
        'julia': 'tree_sitter_julia',
        'bash': 'tree_sitter_bash',
    }
    for ext, lang_name in _LANG_MAP.items():
        pkg_name = _TS_PKG.get(lang_name)
        if not pkg_name:
            continue
        try:
            mod = __import__(pkg_name)
            if hasattr(mod, 'language'):
                lang_obj = mod.language()
            elif hasattr(mod, 'language_' + lang_name.replace('-', '_')):
                lang_obj = getattr(mod, 'language_' + lang_name.replace('-', '_'))()
            elif hasattr(mod, f'language_{lang_name}'):
                lang_obj = getattr(mod, f'language_{lang_name}')()
            else:
                continue
            langs[ext] = (lang_obj, lang_name)
        except Exception:
            continue
    return langs

if _TS_AVAILABLE:
    _TREESITTER_LANGS.update(_init_tree_sitter())

# #[Language config]
LANGUAGE_EXTENSIONS: dict[str, str] = {
    '.py': 'python',
    '.js': 'javascript', '.ts': 'typescript', '.jsx': 'jsx', '.tsx': 'tsx',
    '.mjs': 'javascript', '.cjs': 'javascript',
    '.go': 'go', '.rs': 'rust', '.java': 'java',
    '.c': 'c', '.cpp': 'cpp', '.h': 'c', '.hpp': 'cpp',
    '.rb': 'ruby', '.cs': 'c-sharp', '.kt': 'kotlin', '.swift': 'swift',
    '.php': 'php', '.scala': 'scala', '.lua': 'luau', '.zig': 'zig',
    '.dart': 'dart', '.jl': 'julia',
    '.sh': 'bash', '.bash': 'bash', '.zsh': 'bash',
    '.vue': 'vue', '.svelte': 'svelte', '.astro': 'astro',
}

# #[Data Models]

@dataclass
class Decorator:
    line: int
    name: str

@dataclass
class FunctionInfo:
    name: str
    line: int
    end_line: int
    params: str
    return_annotation: str
    decorators: list[Decorator] = field(default_factory=list)
    is_async: bool = False
    docstring_first_line: Optional[str] = None

    def signature_display(self) -> str:
        ra = self.return_annotation
        if ra:
            return f"{self.name}({self.params}){ra}"
        return f"{self.name}({self.params})"

@dataclass
class ClassInfo:
    name: str
    line: int
    end_line: int
    bases: list[str] = field(default_factory=list)
    decorators: list[Decorator] = field(default_factory=list)
    docstring_first_line: Optional[str] = None
    methods: list[FunctionInfo] = field(default_factory=list)

@dataclass
class ImportInfo:
    module: str
    names: list[str]
    line: int
    end_line: int
    category: str  # 'stdlib' | 'third_party' | 'local'

@dataclass
class ConstantInfo:
    name: str
    line: int
    value_summary: str = ''

@dataclass
class SectionMarker:
    level: int   # 1=[Section], 2=[SubSection], 3=[SubSubSection]
    line: int
    title: str

@dataclass
class TodoItem:
    line: int
    kind: str
    text: str

@dataclass
class FileMap:
    source_path: str
    sha256: str
    lines: int
    size_bytes: int
    timestamp: str
    language: str = 'python'
    module_docstring: Optional[str] = None
    imports: list[ImportInfo] = field(default_factory=list)
    constants: list[ConstantInfo] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)
    section_markers: list[SectionMarker] = field(default_factory=list)
    todos: list[TodoItem] = field(default_factory=list)

    @property
    def total_symbols(self) -> int:
        return len(self.classes) + len(self.functions)

# ##[Graph & Cost Data Models]

@dataclass
class DependencyEdge:
    source: str
    target: str
    line: int = 0
    kind: str = 'import'
    name: str = ''

@dataclass
class ProjectGraph:
    nodes: dict[str, FileMap] = field(default_factory=dict)
    edges: list[DependencyEdge] = field(default_factory=list)

    @property
    def total_edges(self) -> int:
        return len(self.edges)

    @property
    def total_nodes(self) -> int:
        return len(self.nodes)

@dataclass
class CostRecord:
    date: str
    operation: str
    files_processed: int
    tokens_saved: int

class CostTracker:
    def __init__(self, path: Path):
        self.path = path
        self.records: list[CostRecord] = []
        self.total_saved: int = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.total_saved = data.get('total_saved', 0)
            for r in data.get('records', []):
                self.records.append(CostRecord(**r))
        except Exception:
            pass

    def add(self, operation: str, files_processed: int, tokens_saved: int) -> CostRecord:
        r = CostRecord(
            date=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            operation=operation,
            files_processed=files_processed,
            tokens_saved=tokens_saved,
        )
        self.records.append(r)
        self.total_saved += tokens_saved
        self._save()
        return r

    def _save(self) -> None:
        data = {
            'total_saved': self.total_saved,
            'records': [
                {'date': r.date, 'operation': r.operation,
                 'files_processed': r.files_processed, 'tokens_saved': r.tokens_saved}
                for r in self.records
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding='utf-8')

    def summary(self) -> str:
        return f"Total tokens saved: {self.total_saved:,} across {len(self.records)} operations"


# ##[AST Analysis Utilities]

def _get_module_docstring(tree: ast.Module) -> Optional[str]:
    if (tree.body
            and isinstance(tree.body[0], ast.Expr)
            and isinstance(tree.body[0].value, ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        doc = tree.body[0].value.value.strip()
        first = doc.split('\n')[0].strip()
        if len(first) > 90:
            first = first[:87] + '...'
        return first if first else None
    return None

def _get_node_docstring_first_line(node: ast.AsyncFunctionDef | ast.FunctionDef | ast.ClassDef) -> Optional[str]:
    if (node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)):
        doc = node.body[0].value.value.strip()
        first = doc.split('\n')[0].strip()
        if len(first) > 90:
            first = first[:87] + '...'
        return first if first else None
    return None

def _truncate(val: str, max_len: int = 60) -> str:
    if len(val) > max_len:
        return val[:max_len - 3] + '...'
    return val

def _build_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, str]:
    """Return (params_str, return_annotation_str)."""
    a = node.args
    params: list[str] = []

    all_pos = a.posonlyargs + a.args
    n_posonly = len(a.posonlyargs)
    n_defaults = len(a.defaults)
    default_offset = len(all_pos) - n_defaults if n_defaults else len(all_pos) + 1

    for i, arg in enumerate(all_pos):
        if i >= default_offset:
            d_idx = i - default_offset
            params.append(f"{arg.arg}={ast.unparse(a.defaults[d_idx])}")
        else:
            params.append(arg.arg)

    if n_posonly:
        params.insert(n_posonly, '/')

    if a.vararg:
        params.append(f"*{a.vararg.arg}")

    if a.kwonlyargs and not a.vararg:
        has_star = any(p.startswith('*') for p in params)
        if not has_star:
            params.append('*')

    for i, arg in enumerate(a.kwonlyargs):
        if i < len(a.kw_defaults) and a.kw_defaults[i] is not None:
            params.append(f"{arg.arg}={ast.unparse(a.kw_defaults[i])}")
        else:
            params.append(arg.arg)

    if a.kwarg:
        params.append(f"**{a.kwarg.arg}")

    params_str = ", ".join(params)
    ret_str = ""
    if node.returns:
        try:
            ret = ast.unparse(node.returns)
            ret_str = f" -> {ret}"
        except Exception:
            ret_str = " -> ?"

    return params_str, ret_str


# ##[AST Visitor]

class PythonAnalyzer(ast.NodeVisitor):
    def __init__(self, source_text: str):
        self.source_text = source_text
        self.classes: list[ClassInfo] = []
        self.functions: list[FunctionInfo] = []
        self.imports: list[ImportInfo] = []
        self.constants: list[ConstantInfo] = []
        self._curr_class: ClassInfo | None = None
        self._func_depth: int = 0
        self._class_depth: int = 0

    def _capture_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, is_async: bool = False) -> None:
        params_str, ret_str = _build_signature(node)
        decos = [Decorator(line=d.lineno, name=_truncate(ast.unparse(d), 48)) for d in node.decorator_list]
        fn = FunctionInfo(
            name=node.name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            params=params_str,
            return_annotation=ret_str,
            decorators=decos,
            is_async=is_async,
            docstring_first_line=_get_node_docstring_first_line(node),
        )
        if self._curr_class is not None:
            self._curr_class.methods.append(fn)
        else:
            self.functions.append(fn)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._func_depth > 0:
            return
        bases = [ast.unparse(b) for b in node.bases]
        decos = [Decorator(line=d.lineno, name=ast.unparse(d)) for d in node.decorator_list]
        cls = ClassInfo(
            name=node.name,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            bases=bases,
            decorators=decos,
            docstring_first_line=_get_node_docstring_first_line(node),
        )
        parent = self._curr_class
        self._curr_class = cls
        self._class_depth += 1
        self.generic_visit(node)
        self._class_depth -= 1
        self._curr_class = parent
        self.classes.append(cls)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._func_depth > 0:
            return
        self._func_depth += 1
        self._capture_function(node, is_async=False)
        self._func_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        if self._func_depth > 0:
            return
        self._func_depth += 1
        self._capture_function(node, is_async=True)
        self._func_depth -= 1

    def _categorize_import(self, module_name: str, source_dir: Path) -> str:
        if module_name.startswith('.'):
            return 'local'
        top = module_name.split('.')[0]
        if top in STDLIB_MODULES:
            return 'stdlib'
        local_path = source_dir / f"{top}.py"
        if local_path.exists():
            return 'local'
        local_pkg = source_dir / top
        if local_pkg.is_dir() and (local_pkg / '__init__.py').exists():
            return 'local'
        return 'third_party'

    def visit_Import(self, node: ast.Import) -> None:
        if self._func_depth > 0 or self._class_depth > 0:
            return
        for alias in node.names:
            src_path = Path(getattr(node, '_source_path', '.'))
            cat = self._categorize_import(alias.name, src_path.parent if src_path != '.' else Path('.'))
            self.imports.append(ImportInfo(
                module=alias.name,
                names=[alias.asname or alias.name] if alias.asname else [alias.name],
                line=node.lineno,
                end_line=node.end_lineno or node.lineno,
                category=cat,
            ))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._func_depth > 0 or self._class_depth > 0:
            return
        module = node.module or ''
        names = [a.name or a.asname or '' for a in node.names]
        src_path = Path(getattr(node, '_source_path', '.'))
        cat = self._categorize_import(module, src_path.parent if src_path != '.' else Path('.'))
        self.imports.append(ImportInfo(
            module=module,
            names=names,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            category=cat,
        ))

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._func_depth > 0 or self._class_depth > 0:
            return
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                val = _truncate(ast.unparse(node.value), 60)
                self.constants.append(ConstantInfo(name=target.id, line=node.lineno, value_summary=val))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._func_depth > 0 or self._class_depth > 0:
            return
        if isinstance(node.target, ast.Name) and node.target.id.isupper() and node.value:
            val = _truncate(ast.unparse(node.value), 60)
            self.constants.append(ConstantInfo(name=node.target.id, line=node.lineno, value_summary=val))


# ##[Source Scanning]

_SECTION_RE = re.compile(r'(?:#|//)\s*(#{1,3})\s*\[([^\]]+)\]')
_TODO_RE = re.compile(r'#.*\b(TODO|FIXME|HACK|XXX|BUG|WORKAROUND|OPTIMIZE|NOTE)\b\s*[:\-]?\s*(.*?)$')
_LEVEL_MAP = {'#': 1, '##': 2, '###': 3}

def _scan_section_markers(lines: list[str]) -> list[SectionMarker]:
    markers: list[SectionMarker] = []
    for i, line in enumerate(lines, start=1):
        m = _SECTION_RE.search(line)
        if m:
            level = _LEVEL_MAP.get(m.group(1), 1)
            title = m.group(2).strip()
            markers.append(SectionMarker(level=level, line=i, title=title))
    return markers

def _scan_todos(lines: list[str]) -> list[TodoItem]:
    todos: list[TodoItem] = []
    for i, line in enumerate(lines, start=1):
        m = _TODO_RE.search(line)
        if m:
            kind = m.group(1)
            text = m.group(2).strip()
            if len(text) > 90:
                text = text[:87] + '...'
            todos.append(TodoItem(line=i, kind=kind, text=text))
    return todos

def _scan_simple_funcs(lines: list[str]) -> list[FunctionInfo]:
    """Regex-based fallback for non-Python, non-tree-sitter files."""
    funcs: list[FunctionInfo] = []
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        m = re.match(r'^(?:export\s+)?(?:async\s+)?(?:function\s+)?(\w+)\s*\(', s)
        if m and not s.startswith(('#', '//', '/*', '*', '<!--')):
            name = m.group(1)
            if name not in ('if', 'for', 'while', 'switch', 'catch', 'return', 'elif'):
                funcs.append(FunctionInfo(name=name, line=i, end_line=i, params='...', return_annotation=''))
    # Deduplicate by name
    seen: set[str] = set()
    deduped: list[FunctionInfo] = []
    for f in funcs:
        if f.name not in seen:
            seen.add(f.name)
            deduped.append(f)
    return deduped

def _scan_simple_classes(lines: list[str]) -> list[ClassInfo]:
    classes: list[ClassInfo] = []
    for i, line in enumerate(lines, start=1):
        s = line.strip()
        m = re.match(r'^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)', s)
        if m and not s.startswith(('#', '//', '/*')):
            classes.append(ClassInfo(name=m.group(1), line=i, end_line=i))
    return classes

def _scan_todos_any_comment(lines: list[str]) -> list[TodoItem]:
    """Scan for TODOs using // or # or <!-- comments."""
    todos: list[TodoItem] = []
    todo_re = re.compile(r'(?://|#|<!--)\s*\b(TODO|FIXME|HACK|XXX|BUG|NOTE)\b\s*[:\-]?\s*(.*?)(?:-->)?$', re.IGNORECASE)
    for i, line in enumerate(lines, start=1):
        m = todo_re.search(line)
        if m:
            kind = m.group(1).upper()
            text = m.group(2).strip()
            if len(text) > 90:
                text = text[:87] + '...'
            todos.append(TodoItem(line=i, kind=kind, text=text))
    return todos

# ##[Multi-Language Analyzer]

def analyze_python_file(path: Path, source_text: str, source_lines: list[str]) -> FileMap:
    tree = ast.parse(source_text, filename=str(path))
    analyzer = PythonAnalyzer(source_text)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            node._source_path = str(path)

    analyzer.visit(tree)

    sha = _sha256_of_text(source_text)
    n_lines = len(source_lines)
    size = len(source_text.encode('utf-8'))
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    doc = _get_module_docstring(tree)
    sections = _scan_section_markers(source_lines)
    todos = _scan_todos(source_lines)

    return FileMap(
        source_path=str(path),
        sha256=sha,
        lines=n_lines,
        size_bytes=size,
        timestamp=ts,
        language='python',
        module_docstring=doc,
        imports=analyzer.imports,
        constants=analyzer.constants,
        classes=analyzer.classes,
        functions=analyzer.functions,
        section_markers=sections,
        todos=todos,
    )

def _ts_query_capture(ext: str, node: Any, capture_name: str) -> list[Any]:
    """Run a tree-sitter query on a node and return captured nodes by name."""
    try:
        from tree_sitter import Query
    except ImportError:
        return []

    qs = _TS_QUERIES.get(ext, {}).get(capture_name)
    if not qs:
        return []
    results: list[Any] = []
    for q_source in qs:
        try:
            q = Query(_TREESITTER_LANGS[ext][0].language, q_source)
            captures = q.captures(node)
            for cap_node, cap_tag in captures:
                if cap_tag.decode() == capture_name:
                    results.append(cap_node)
        except Exception:
            continue
    return results

_TS_QUERIES: dict[str, dict[str, list[str]]] = {}

def _build_ts_queries() -> None:
    """Build tree-sitter S-exp queries per language."""
    func_query = """
(function_definition
  name: (identifier) @function_name)
"""
    class_query = """
(class_declaration
  name: (identifier) @class_name)
"""
    for ext, (lang_obj, lang_name) in _TREESITTER_LANGS.items():
        _TS_QUERIES[ext] = {}
        try:
            _TS_QUERIES[ext]['function_name'] = [func_query]
            _TS_QUERIES[ext]['class_name'] = [class_query]
        except Exception:
            pass

if _TS_AVAILABLE:
    _build_ts_queries()

def analyze_with_treesitter(path: Path, source_text: str, source_lines: list[str]) -> Optional[FileMap]:
    ext = path.suffix
    if ext not in _TREESITTER_LANGS or ext in ('.py',):
        return None
    lang_obj, lang_name = _TREESITTER_LANGS[ext]
    try:
        tree = _ts_lib.Tree(lang_obj.language, source_text.encode('utf-8'))
    except Exception:
        return None
    root = tree.root_node
    if root is None:
        return None

    classes: list[ClassInfo] = []
    funcs: list[FunctionInfo] = []
    consts: list[ConstantInfo] = []

    for cap_node in _ts_query_capture(ext, root, 'function_name'):
        start = cap_node.start_point[0] + 1
        end = cap_node.end_point[0] + 1
        name = source_text[cap_node.start_byte:cap_node.end_byte]
        funcs.append(FunctionInfo(name=name, line=start, end_line=end, params='', return_annotation=''))

    for cap_node in _ts_query_capture(ext, root, 'class_name'):
        start = cap_node.start_point[0] + 1
        end = cap_node.end_point[0] + 1
        name = source_text[cap_node.start_byte:cap_node.end_byte]
        classes.append(ClassInfo(name=name, line=start, end_line=end))

    # Scan for UPPER_CASE constants by regex
    for i, line in enumerate(source_lines, start=1):
        m = re.match(r'^\s*(?:export\s+)?(?:const|let|var|val)\s+([A-Z][A-Z0-9_]+)\s*[=:]', line)
        if m:
            val = line.split('=')[-1].strip() if '=' in line else ''
            consts.append(ConstantInfo(name=m.group(1), line=i, value_summary=_truncate(val, 40)))

    imports: list[ImportInfo] = []
    for i, line in enumerate(source_lines, start=1):
        m = re.match(r'^\s*(?:import|from)\s+[\'\"]([^\'\"]+)[\'\"]', line)
        if m:
            imports.append(ImportInfo(module=m.group(1), names=[m.group(1).split('/')[-1]], line=i, end_line=i, category='third_party'))
        m = re.match(r'^\s*import\s+(\w+)', line)
        if m and not line.strip().startswith('#'):
            imports.append(ImportInfo(module=m.group(1), names=[m.group(1)], line=i, end_line=i, category='third_party'))

    sections = _scan_section_markers(source_lines)
    todos = _scan_todos_any_comment(source_lines)

    sha = _sha256_of_text(source_text)
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    n_lines = len(source_lines)
    size = len(source_text.encode('utf-8'))

    return FileMap(
        source_path=str(path),
        sha256=sha,
        lines=n_lines,
        size_bytes=size,
        timestamp=ts,
        language=lang_name,
        imports=imports,
        constants=consts,
        classes=classes,
        functions=funcs,
        section_markers=sections,
        todos=todos,
    )

def analyze_file(path: Path, source_text: str, source_lines: list[str]) -> FileMap:
    ext = path.suffix
    if ext == '.py':
        return analyze_python_file(path, source_text, source_lines)
    if _TS_AVAILABLE and ext in _TREESITTER_LANGS:
        result = analyze_with_treesitter(path, source_text, source_lines)
        if result is not None:
            return result
    # Fallback to regex analysis
    sha = _sha256_of_text(source_text)
    n_lines = len(source_lines)
    size = len(source_text.encode('utf-8'))
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    funcs = _scan_simple_funcs(source_lines)
    classes = _scan_simple_classes(source_lines)
    todos = _scan_todos_any_comment(source_lines)
    sections = _scan_section_markers(source_lines)
    lang = LANGUAGE_EXTENSIONS.get(ext, 'unknown')
    return FileMap(
        source_path=str(path), sha256=sha, lines=n_lines, size_bytes=size,
        timestamp=ts, language=lang,
        functions=funcs, classes=classes, todos=todos, section_markers=sections,
    )


# ##[Hashing & File Utilities]

def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]

def sha256_file(path: Path) -> str:
    text, _ = read_source(path)
    return _sha256_of_text(text)

def count_lines(path: Path) -> int:
    with open(path, 'rb') as f:
        return sum(1 for _ in f)

def get_file_size(path: Path) -> int:
    return path.stat().st_size

def read_source(path: Path) -> tuple[str, list[str]]:
    raw = path.read_bytes()
    if raw[:3] == b'\xef\xbb\xbf':
        raw = raw[3:]
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        text = raw.decode('latin-1')
    lines = text.splitlines(keepends=True)
    return text, lines


# ##[Gitignore Support]

def load_gitignore_patterns(path: Path) -> list[re.Pattern]:
    patterns: list[re.Pattern] = []
    gitignore = path / '.gitignore'
    gignore = path / '.llmeshowyouignore'
    for f in [gitignore, gignore]:
        if f.exists():
            try:
                for line in f.read_text(encoding='utf-8').splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if line.startswith('!'):
                        continue
                    patterns.append(re.compile(fnmatch_to_re(line)))
            except Exception:
                pass
    return patterns

def fnmatch_to_re(pattern: str) -> str:
    """Convert a gitignore-style glob to a regex."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == '*':
            if i + 1 < len(pattern) and pattern[i + 1] == '*':
                parts.append('.*')
                if i + 2 < len(pattern) and pattern[i + 2] == '/':
                    i += 3
                    continue
                i += 2
                continue
            parts.append('[^/]*')
        elif c == '?':
            parts.append('[^/]')
        elif c == '.':
            parts.append('\\.')
        else:
            parts.append(re.escape(c))
        i += 1
    return '^' + ''.join(parts) + '$'

def matches_gitignore(file: Path, patterns: list[re.Pattern], root: Path) -> bool:
    try:
        rel = str(file.relative_to(root))
    except ValueError:
        rel = str(file)
    for pat in patterns:
        if pat.search(rel):
            return True
        if pat.search(file.name):
            return True
    return False


# ##[Markdown Rendering]

def _fmt_decorators(decos: list[Decorator]) -> str:
    if not decos:
        return ''
    parts = []
    for d in decos:
        name = d.name
        if name.startswith('@'):
            parts.append(name)
        else:
            parts.append(f"@{name}")
    return ' '.join(parts) + ' '

def render_map(fm: FileMap) -> str:
    return _render_clean(fm)

def _render_clean(fm: FileMap) -> str:
    buf: list[str] = []

    buf.append(f"# Map: {fm.source_path}")
    buf.append(f"meta: {fm.lines} lines | sha256:{fm.sha256} | mapped {fm.timestamp} | lang:{fm.language}")
    buf.append('')
    buf.append(
        "<!-- LLM READ PROTOCOL: This map is a token-efficient index of the source. "
        "Use the line ranges below to read ONLY the specific function/class/section you need "
        f"(e.g. Read {fm.source_path} offset=<start> limit=<end-start+1>). "
        "Do NOT read the entire source file -- doing so defeats the purpose of this map and "
        "costs ~10x the tokens. Read the full file only if a needed range is unclear AFTER "
        "consulting this map. -->")
    buf.append('')

    if fm.module_docstring:
        buf.append(f"> {fm.module_docstring}")
        buf.append('')

    # ##[Imports]
    if fm.imports:
        buf.append("## Imports")
        for cat_label, cat_key in [('stdlib', 'stdlib'), ('third-party', 'third_party'), ('local', 'local')]:
            items = [i for i in fm.imports if i.category == cat_key]
            if not items:
                continue
            min_ln = min(i.line for i in items)
            max_ln = max(i.end_line for i in items)
            names: list[str] = []
            for imp in items:
                if imp.module and imp.names:
                    for n in imp.names:
                        if n != imp.module and not n.startswith('.'):
                            names.append(n)
                        elif n == imp.module:
                            names.append(imp.module)
                elif imp.module:
                    names.append(imp.module)
            unique = sorted(set(names))
            buf.append(f"  **{cat_label}** [{min_ln}-{max_ln}]: {', '.join(unique)}")
        buf.append('')

    # ##[Constants]
    if fm.constants:
        buf.append("## Constants")
        parts = []
        for c in fm.constants:
            label = c.name
            if c.value_summary:
                label = f"{c.name} = {c.value_summary}"
            parts.append(f"{label} \u00b7 {c.line}")
        line = "  " + "  ".join(parts)
        buf.append(line)
        buf.append('')

    # ##[Classes]
    if fm.classes:
        buf.append("## Classes")
        for cls in fm.classes:
            dec_s = _fmt_decorators(cls.decorators)
            base_s = f"({', '.join(cls.bases)}) " if cls.bases else ''
            doc_s = f'  "{cls.docstring_first_line}"' if cls.docstring_first_line else ''
            buf.append(f"### {dec_s}{cls.name}  [{cls.line}-{cls.end_line}]  {base_s}{doc_s}")
            for m in cls.methods:
                dec_s = _fmt_decorators(m.decorators)
                async_s = "async " if m.is_async else ''
                doc_s = f'  "{m.docstring_first_line}"' if m.docstring_first_line else ''
                buf.append(f"  {dec_s}{async_s}{m.signature_display()}  [{m.line}-{m.end_line}]{doc_s}")
        buf.append('')

    # ##[Functions]
    if fm.functions:
        buf.append("## Functions")
        for fn in fm.functions:
            dec_s = _fmt_decorators(fn.decorators)
            async_s = "async " if fn.is_async else ''
            doc_s = f'  "{fn.docstring_first_line}"' if fn.docstring_first_line else ''
            buf.append(f"  {dec_s}{async_s}{fn.signature_display()}  [{fn.line}-{fn.end_line}]{doc_s}")
        buf.append('')

    # ##[Section Markers]
    if fm.section_markers:
        buf.append("## Sections")
        for m in fm.section_markers:
            indent = "  " * (m.level - 1)
            prefix = "#" * m.level
            buf.append(f"{indent}{prefix}[{m.title}]  [{m.line}]")
        buf.append('')

    # ##[TODOs / FIXMEs]
    if fm.todos:
        buf.append(f"## TODOs / FIXMEs  ({len(fm.todos)})")
        for t in fm.todos:
            buf.append(f"  L{t.line} {t.kind}: {t.text}")
        buf.append('')

    return '\n'.join(buf)


# ##[Map Persistence & Parsing]

MAP_SUFFIX = '.map.md'

def map_path_for(source: Path) -> Path:
    return source.with_suffix(source.suffix + MAP_SUFFIX)

def write_map(fm: FileMap, output_path: Path) -> None:
    output_path.write_text(render_map(fm), encoding='utf-8')

def read_stored_hash(map_path: Path) -> Optional[str]:
    try:
        first = map_path.read_text(encoding='utf-8').splitlines()
        for line in first[:5]:
            m = re.search(r'sha256:([a-f0-9]+)', line)
            if m:
                return m.group(1)
    except Exception:
        return None
    return None

def parse_existing_symbols(text: str) -> tuple[int, set[str]]:
    lines = 0
    symbols: set[str] = set()

    m = re.search(r'meta:\s*(\d+)\s*lines', text)
    if m:
        lines = int(m.group(1))

    for line in text.splitlines():
        cm = re.match(r'^###\s+(?:@\S+\s+)*(\w+)\s+\[', line)
        if cm:
            symbols.add(f"class:{cm.group(1)}")
            continue
    in_functions = False
    for line in text.splitlines():
        if line.startswith('## Functions'):
            in_functions = True
            continue
        if line.startswith('## '):
            in_functions = False
            continue
        if in_functions:
            if line.strip().startswith('@'):
                continue
            fm = re.match(r'^(?:async\s+)?(\w+)\s*\(', line.strip())
            if fm:
                symbols.add(f"func:{fm.group(1)}")

    return lines, symbols


# ##[Change Detection]

def is_stale(source_path: Path, map_path: Path) -> bool:
    if not map_path.exists():
        return True
    stored = read_stored_hash(map_path)
    if stored is None:
        return True
    current = sha256_file(source_path)
    return current != stored

def is_major_change(source_path: Path, map_path: Path, fm: FileMap) -> bool:
    if not map_path.exists():
        return True
    try:
        existing = map_path.read_text(encoding='utf-8')
    except Exception:
        return True

    old_lines, old_symbols = parse_existing_symbols(existing)

    if old_lines > 0:
        change_pct = abs(fm.lines - old_lines) / old_lines
        if change_pct > 0.20:
            return True

    new_symbols: set[str] = set()
    for c in fm.classes:
        new_symbols.add(f"class:{c.name}")
    for f in fm.functions:
        new_symbols.add(f"func:{f.name}")

    return old_symbols != new_symbols


# ##[File Discovery]

SUPPORTED_EXTENSIONS = set(LANGUAGE_EXTENSIONS.keys())

def find_source_files(patterns: list[str], recursive: bool = False, extra_exts: Optional[set[str]] = None,
                       gitignore_root: Optional[Path] = None) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    all_exts: set[str] = {'.py'}
    if extra_exts is not None:
        all_exts |= extra_exts
    gi_patterns = load_gitignore_patterns(gitignore_root) if gitignore_root else []

    for pat in patterns:
        p = Path(pat)
        if p.is_dir():
            if recursive:
                for ext in all_exts:
                    candidates = list(p.rglob(f'*{ext}'))
                    for c in candidates:
                        if c not in seen and not matches_gitignore(c, gi_patterns, p):
                            seen.add(c)
                            files.append(c)
            else:
                for ext in all_exts:
                    candidates = list(p.glob(f'*{ext}'))
                    for c in candidates:
                        if c not in seen and not matches_gitignore(c, gi_patterns, p):
                            seen.add(c)
                            files.append(c)
        else:
            expanded = glob.glob(pat)
            if not expanded:
                for ext in all_exts:
                    expanded = glob.glob(pat + ext)
                    if expanded:
                        break
            for m in expanded:
                mp = Path(m)
                if mp not in seen:
                    seen.add(mp)
                    files.append(mp)
    return sorted(files)

# Compatibility alias
find_py_files = find_source_files


# ##[Project Graph Construction]

def build_project_graph(maps: list[FileMap]) -> ProjectGraph:
    g = ProjectGraph()
    file_map_by_name: dict[str, FileMap] = {}
    name_to_fm: dict[str, FileMap] = {}

    for fm in maps:
        p = Path(fm.source_path)
        g.nodes[str(p)] = fm
        stem = p.stem
        if stem in file_map_by_name:
            file_map_by_name[stem] = fm
            name_to_fm[p.name] = fm
        else:
            file_map_by_name[stem] = fm
            name_to_fm[p.name] = fm

    for fm in maps:
        src_path = Path(fm.source_path)
        for imp in fm.imports:
            if imp.category != 'local':
                continue
            target_stem = imp.module.split('.')[0]
            target = file_map_by_name.get(target_stem)
            if target:
                g.edges.append(DependencyEdge(
                    source=str(src_path),
                    target=str(Path(target.source_path)),
                    line=imp.line,
                    kind='import' if imp.module == target_stem else 'import_from',
                    name=imp.module,
                ))
    return g

def rank_god_nodes(graph: ProjectGraph) -> list[tuple[str, int]]:
    in_degree: defaultdict[str, int] = defaultdict(int)
    for edge in graph.edges:
        if edge.target in graph.nodes:
            in_degree[edge.target] += 1
    return sorted(in_degree.items(), key=lambda x: -x[1])


# ##[HTML Graph Renderer]

def render_html_index(graph: ProjectGraph, output_path: Path) -> None:
    buf = StringIO()
    buf.write('''<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>llmeshowyou -- Project Graph</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
       background:#f5f5f5; color:#1e1e1e; padding:24px; }
h1 { font-size:22px; margin-bottom:4px; }
.meta { color:#666; font-size:13px; margin-bottom:20px; }
.god-nodes { background:#fff; border:1px solid #ddd; border-radius:8px; padding:16px; margin-bottom:20px; }
.god-nodes h2 { font-size:16px; margin-bottom:10px; }
.god-nodes ol { padding-left:20px; }
.god-nodes li { padding:4px 0; font-size:13px; }
.god-nodes .count { color:#666; }
.graph { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:12px; }
.node-card { background:#fff; border:1px solid #ddd; border-radius:8px; padding:14px; }
.node-card h3 { font-size:14px; margin-bottom:6px; }
.node-card .lang { font-size:11px; color:#666; background:#e8e8e8; border-radius:3px; padding:1px 6px; display:inline-block; margin-left:6px; }
.node-card .stats { font-size:12px; color:#555; margin-bottom:6px; }
.node-card .edges { font-size:12px; }
.node-card .edges li { margin:2px 0 2px 14px; color:#1565c0; list-style:disc; }
.node-card .incoming { color:#2e7d32; font-weight:500; }
.footer { text-align:center; margin-top:30px; font-size:12px; color:#999; }
</style></head><body>
''')
    buf.write(f'<h1>llmeshowyou -- Project Graph</h1>')
    total_lines = sum(n.lines for n in graph.nodes.values())
    buf.write(f'<p class="meta">{graph.total_nodes} files · {graph.total_edges} edges · {total_lines:,} total lines</p>')

    gods = rank_god_nodes(graph)
    if gods:
        buf.write('<div class="god-nodes"><h2>God Nodes -- Most Imported</h2><ol>')
        for i, (fp, degree) in enumerate(gods[:15], 1):
            fm = graph.nodes.get(fp)
            desc = f'({fm.lines} lines, {fm.total_symbols} sym)' if fm else ''
            buf.write(f'<li><strong>{Path(fp).name}</strong> <span class="count">-- imported {degree} times</span> {desc}</li>')
        buf.write('</ol></div>')

    buf.write('<div class="graph">')
    for fp, fm in sorted(graph.nodes.items()):
        p = Path(fp)
        pname = p.name
        ext = p.suffix
        lang = LANGUAGE_EXTENSIONS.get(ext, '?')
        edges_from = [e for e in graph.edges if e.source == fp]
        edges_to = [e for e in graph.edges if e.target == fp]
        buf.write(f'<div class="node-card">')
        buf.write(f'<h3>{xml_escape(pname)}<span class="lang">{xml_escape(fm.language)}</span></h3>')
        buf.write(f'<div class="stats">{fm.lines} lines · {fm.total_symbols} sym · {fm.size_bytes:,} bytes</div>')
        if edges_to:
            buf.write(f'<div class="edges incoming"><- imported by {len(edges_to)}:</div>')
            for e in edges_to[:8]:
                buf.write(f'<li>{xml_escape(Path(e.source).name)}')
            if len(edges_to) > 8:
                buf.write(f'<li>...+{len(edges_to)-8} more')
        if edges_from:
            buf.write(f'<div class="edges">-> imports {len(edges_from)}:</div>')
            for e in edges_from[:8]:
                buf.write(f'<li>{xml_escape(Path(e.target).name)}')
            if len(edges_from) > 8:
                buf.write(f'<li>...+{len(edges_from)-8} more')
        buf.write('</div>')
    buf.write('</div>')
    buf.write('<div class="footer">Generated by llmeshowyou -- file-to-markdown mapper</div></body></html>')

    output_path.write_text(buf.getvalue(), encoding='utf-8')
    print(f"  graph HTML -> {output_path}")


# ##[Wiki Export]

def render_wiki(graph: ProjectGraph, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    index_lines = [
        f'# llmeshowyou -- Wiki',
        f'Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}',
        f'',
        f'**{graph.total_nodes} files · {graph.total_edges} edges**',
        f'',
    ]

    for fp, fm in sorted(graph.nodes.items()):
        p = Path(fp)
        fname = p.stem
        slug = re.sub(r'[^a-zA-Z0-9_-]', '_', fname)
        page_path = output_dir / f'{slug}.md'
        incoming = [e for e in graph.edges if e.target == fp]
        outgoing = [e for e in graph.edges if e.source == fp]

        lines = [
            f'# {fname}',
            f'',
            f'- **Language:** {fm.language}',
            f'- **Lines:** {fm.lines}',
            f'- **Symbols:** {fm.total_symbols}',
            f'- **Path:** `{fp}`',
            f'',
        ]
        if fm.module_docstring:
            lines.append(f'> {fm.module_docstring}')
            lines.append('')
        if incoming:
            lines.append(f'## Imported by ({len(incoming)})')
            for e in incoming:
                inc_name = Path(e.source).stem
                lines.append(f'- [[{inc_name}]]')
            lines.append('')
        if outgoing:
            lines.append(f'## Imports ({len(outgoing)})')
            for e in outgoing:
                out_name = Path(e.target).stem
                lines.append(f'- [[{out_name}]]')
            lines.append('')
        if fm.classes:
            lines.append(f'## Classes')
            for c in fm.classes:
                lines.append(f'- `{c.name}`  [{c.line}-{c.end_line}]')
            lines.append('')
        if fm.functions:
            lines.append(f'## Functions')
            for fn in fm.functions:
                lines.append(f'- `{fn.name}({fn.params})`  [{fn.line}-{fn.end_line}]')
            lines.append('')

        page_path.write_text('\n'.join(lines), encoding='utf-8')
        index_lines.append(f'- [{fname}]({slug}.md) -- {fm.lines} lines')

    index_path = output_dir / 'index.md'
    index_path.write_text('\n'.join(index_lines), encoding='utf-8')
    print(f"  wiki index -> {index_path}")
    print(f"  {len(graph.nodes)} pages written to {output_dir}")


# ##[Export: GraphML]

def export_graphml(graph: ProjectGraph, output_path: Path) -> None:
    buf = StringIO()
    buf.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    buf.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns"\n')
    buf.write('  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n')
    buf.write('  <key id="label" for="node" attr.name="label" attr.type="string"/>\n')
    buf.write('  <key id="lang" for="node" attr.name="language" attr.type="string"/>\n')
    buf.write('  <key id="lines" for="node" attr.name="lines" attr.type="int"/>\n')
    buf.write('  <key id="type" for="edge" attr.name="type" attr.type="string"/>\n')
    buf.write('  <graph id="G" edgedefault="directed">\n')

    for fp, fm in graph.nodes.items():
        nid = xml_escape(fp)
        label = xml_escape(Path(fp).name)
        lang = xml_escape(fm.language)
        buf.write(f'    <node id="{nid}">\n')
        buf.write(f'      <data key="label">{label}</data>\n')
        buf.write(f'      <data key="lang">{lang}</data>\n')
        buf.write(f'      <data key="lines">{fm.lines}</data>\n')
        buf.write(f'    </node>\n')

    for edge in graph.edges:
        src = xml_escape(edge.source)
        tgt = xml_escape(edge.target)
        kind = xml_escape(edge.kind)
        buf.write(f'    <edge source="{src}" target="{tgt}">\n')
        buf.write(f'      <data key="type">{kind}</data>\n')
        buf.write(f'    </edge>\n')

    buf.write('  </graph>\n</graphml>\n')
    output_path.write_text(buf.getvalue(), encoding='utf-8')
    print(f"  graphml -> {output_path}")


# ##[Export: Neo4j Cypher]

def export_neo4j_cypher(graph: ProjectGraph, output_path: Path) -> None:
    buf = StringIO()
    buf.write(f'// llmeshowyou -- Neo4j Cypher Export\n')
    buf.write(f'// {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}\n')
    buf.write(f'// {graph.total_nodes} nodes, {graph.total_edges} edges\n\n')

    for fp, fm in graph.nodes.items():
        p = Path(fp)
        label = p.name
        safe_fp = fp.replace('\\', '/')
        buf.write(f'MERGE (f:File {{path: "{safe_fp}"}})\n')
        buf.write(f'SET f.name = "{label}", f.language = "{fm.language}", f.lines = {fm.lines}\n')

    for edge in graph.edges:
        safe_src = edge.source.replace('\\', '/')
        safe_tgt = edge.target.replace('\\', '/')
        buf.write(f'MATCH (a:File {{path: "{safe_src}"}})\n')
        buf.write(f'MATCH (b:File {{path: "{safe_tgt}"}})\n')
        buf.write(f'MERGE (a)-[:IMPORTS {{type: "{edge.kind}"}}]->(b)\n')

    output_path.write_text(buf.getvalue(), encoding='utf-8')
    print(f"  cypher -> {output_path}")


# ##[Query / Path / Explain]

def query_graph(graph: ProjectGraph, question: str, mode: str = 'bfs', budget: int = 1000) -> str:
    """Simple graph traversal answering a question by finding relevant nodes."""
    keywords = set(re.findall(r'\b[A-Z][a-zA-Z0-9_]{2,}\b', question))
    keywords_lower = set(w.lower() for w in re.findall(r'\b[a-z][a-zA-Z0-9_]{2,}\b', question))

    # Score nodes by keyword match
    scored: list[tuple[float, str, FileMap]] = []
    for fp, fm in graph.nodes.items():
        score = 0.0
        name = Path(fp).stem.lower()
        if name in keywords_lower:
            score += 5.0
        for kw in keywords:
            if kw.lower() in fp.lower():
                score += 3.0
            for cls in fm.classes:
                if kw.lower() == cls.name.lower():
                    score += 4.0
            for fn in fm.functions:
                if kw.lower() == fn.name.lower():
                    score += 4.0
            for imp in fm.imports:
                if kw.lower() in imp.module.lower():
                    score += 1.0
        scored.append((score, fp, fm))
    scored.sort(key=lambda x: -x[0])
    top = [(fp, fm) for s, fp, fm in scored if s > 0][:10]

    if not top:
        return f"No nodes matched '{question}' in the project graph."

    lines: list[str] = []
    lines.append(f"## Query: {question}")
    lines.append(f"Found {len(top)} relevant nodes:\n")

    for fp, fm in top:
        p = Path(fp)
        incoming = len([e for e in graph.edges if e.target == fp])
        outgoing = len([e for e in graph.edges if e.source == fp])
        lines.append(f"### {p.name}  ({fm.language}, {fm.lines} lines)")
        lines.append(f"Path: `{fp}`")
        lines.append(f"Symbols: {fm.total_symbols}  |  <- imported by {incoming}  |  -> imports {outgoing}")
        if fm.classes:
            lines.append(f"Classes: {', '.join(c.name for c in fm.classes[:5])}")
        if fm.functions:
            lines.append(f"Functions: {', '.join(fn.name for fn in fm.functions[:5])}")
        lines.append('')

    return '\n'.join(lines)

def shortest_path(graph: ProjectGraph, start: str, end: str) -> str:
    """BFS shortest path between two files in the import graph."""
    # Resolve to paths
    start_path = _resolve_node(graph, start)
    end_path = _resolve_node(graph, end)
    if not start_path:
        return f"Node '{start}' not found."
    if not end_path:
        return f"Node '{end}' not found."

    adj: dict[str, list[str]] = defaultdict(list)
    for e in graph.edges:
        adj[e.source].append(e.target)

    # BFS
    parent: dict[str, Optional[str]] = {start_path: None}
    q = deque([start_path])
    found = False
    while q:
        cur = q.popleft()
        if cur == end_path:
            found = True
            break
        for nb in adj.get(cur, []):
            if nb not in parent:
                parent[nb] = cur
                q.append(nb)
    if not found:
        return f"No path from '{start}' to '{end}'."

    # Reconstruct
    path: list[str] = []
    cur: Optional[str] = end_path
    while cur is not None:
        path.append(Path(cur).name)
        cur = parent[cur]
    path.reverse()

    return f"Path ({len(path)-1} steps): {' -> '.join(path)}"

def _resolve_node(graph: ProjectGraph, name: str) -> Optional[str]:
    """Find a node by name or path fragment."""
    for fp in graph.nodes:
        if fp == name or Path(fp).name == name or Path(fp).stem == name:
            return fp
    for fp in graph.nodes:
        if name.lower() in fp.lower():
            return fp
    return None

def explain_node(graph: ProjectGraph, node: str) -> str:
    """Explain a node: its contents, incoming/outgoing edges, god rank."""
    fp = _resolve_node(graph, node)
    if not fp:
        return f"Node '{node}' not found."
    fm = graph.nodes[fp]
    p = Path(fp)
    incoming = [e for e in graph.edges if e.target == fp]
    outgoing = [e for e in graph.edges if e.source == fp]
    god_rank = rank_god_nodes(graph)
    rank_pos = next((i for i, (f, _) in enumerate(god_rank) if f == fp), None)

    lines: list[str] = []
    lines.append(f"## {p.name}  ({fm.language})")
    lines.append(f"Path: `{fp}`")
    lines.append(f"Lines: {fm.lines}  |  Bytes: {fm.size_bytes:,}  |  Symbols: {fm.total_symbols}")
    if rank_pos is not None:
        lines.append(f"God-node rank: #{rank_pos + 1} (imported by {len(incoming)} files)")

    if fm.module_docstring:
        lines.append(f"Docstring: {fm.module_docstring}")

    lines.append(f"\n### Dependencies ({len(outgoing)} imports)")
    for e in outgoing:
        lines.append(f"- `{e.name}` -> {Path(e.target).name}")

    lines.append(f"\n### Dependents ({len(incoming)} importers)")
    for e in incoming:
        lines.append(f"- {Path(e.source).name}")

    if fm.classes:
        lines.append(f"\n### Classes")
        for c in fm.classes:
            lines.append(f"- `{c.name}` [{c.line}-{c.end_line}]")
            for m in c.methods:
                lines.append(f"  - `{m.name}({m.params})` [{m.line}-{m.end_line}]")

    if fm.functions:
        lines.append(f"\n### Functions")
        for fn in fm.functions:
            lines.append(f"- `{fn.name}({fn.params})` [{fn.line}-{fn.end_line}]")

    if fm.section_markers:
        lines.append(f"\n### Section Markers")
        for m in fm.section_markers:
            prefix = "#" * m.level
            indent = "  " * (m.level - 1)
            lines.append(f"- {indent}{prefix}[{m.title}]  line {m.line}")

    if fm.todos:
        lines.append(f"\n### TODOs / FIXMEs")
        for t in fm.todos:
            lines.append(f"- L{t.line} {t.kind}: {t.text}")

    return '\n'.join(lines)


# ##[MCP Server]

def mcp_serve(graph: ProjectGraph, transport: str = 'stdio', port: int = 8080) -> None:
    """Simple stdio or HTTP MCP server exposing the project graph."""
    if transport == 'http':
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class MCPHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length) if length else b''
                resp = _mcp_handler(body.decode('utf-8'), graph)
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(resp.encode('utf-8'))

            def log_message(self, fmt, *args):
                pass  # quiet

        server = HTTPServer(('127.0.0.1', port), MCPHandler)
        print(f"  MCP HTTP server -> http://127.0.0.1:{port}/mcp")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
    else:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                resp = _mcp_handler(line, graph)
                print(resp, flush=True)
            except json.JSONDecodeError:
                continue

def _mcp_handler(body: str, graph: ProjectGraph) -> str:
    """Handle a single MCP request."""
    try:
        req = json.loads(body)
    except json.JSONDecodeError:
        return json.dumps({'error': 'invalid JSON'})
    method = req.get('method', '')
    params = req.get('params', {})
    rid = req.get('id', 0)

    if method == 'query_graph':
        result = query_graph(graph, params.get('question', ''), params.get('mode', 'bfs'), params.get('budget', 1000))
    elif method == 'get_node':
        fp = _resolve_node(graph, params.get('path', ''))
        if fp:
            result = explain_node(graph, fp)
        else:
            result = f"Node '{params.get('path', '')}' not found."
    elif method == 'get_neighbors':
        fp = _resolve_node(graph, params.get('path', ''))
        if fp:
            inc = [Path(e.source).name for e in graph.edges if e.target == fp]
            out = [Path(e.target).name for e in graph.edges if e.source == fp]
            result = json.dumps({'incoming': inc, 'outgoing': out})
        else:
            result = json.dumps({'error': 'node not found'})
    elif method == 'shortest_path':
        result = shortest_path(graph, params.get('start', ''), params.get('end', ''))
    elif method == 'list_files':
        result = json.dumps(sorted(Path(p).name for p in graph.nodes))
    elif method == 'god_nodes':
        gods = rank_god_nodes(graph)
        result = json.dumps([{'file': Path(f).name, 'imported_by': d} for f, d in gods[:20]])
    else:
        result = json.dumps({'error': f'unknown method: {method}'})

    return json.dumps({'id': rid, 'result': result})


# ##[Cost Tracker]

def load_cost(path: Path) -> CostTracker:
    return CostTracker(path)

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


# ##[Git Hook Management]

HOOK_SCRIPT = '''#!/bin/sh
# llmeshowyou post-commit hook -- auto-regenerate stale .map.md files
# Installed by: llmeshowyou hook install
echo "llmeshowyou: checking for stale maps..."
FILES=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null | grep -E '\\.(py|js|ts|go|rs|java|c|cpp|rb|cs|kt|swift|php)$' || true)
if [ -n "$FILES" ]; then
  for f in $FILES; do
    dir=$(dirname "$f")
    if [ -f "$dir/llmeshowyou.py" ] || [ -f "$dir/../llmeshowyou.py" ]; then
      python3 "$(dirname "$(realpath "$0")")/llmeshowyou.py" update "$dir" --major-only 2>/dev/null || true
    fi
  done
fi
'''

def install_hook(project_dir: Path) -> int:
    hook_dir = project_dir / '.git' / 'hooks'
    if not hook_dir.exists():
        print("llmeshowyou: not a git repository (no .git/hooks).", file=sys.stderr)
        return 1
    hook_path = hook_dir / 'post-commit'
    try:
        hook_path.write_text(HOOK_SCRIPT, encoding='utf-8')
        hook_path.chmod(0o755)
    except Exception as e:
        print(f"llmeshowyou: error installing hook: {e}", file=sys.stderr)
        return 1
    print(f"  hook installed: {hook_path}")
    return 0

def uninstall_hook(project_dir: Path) -> int:
    hook_path = project_dir / '.git' / 'hooks' / 'post-commit'
    if hook_path.exists():
        content = hook_path.read_text(encoding='utf-8')
        if 'llmeshowyou' in content:
            hook_path.unlink()
            print(f"  hook removed: {hook_path}")
            return 0
        else:
            print(f"  not an llmeshowyou hook; skipping.")
    else:
        print(f"  no hook found.")
    return 0

def status_hook(project_dir: Path) -> int:
    hook_path = project_dir / '.git' / 'hooks' / 'post-commit'
    if hook_path.exists():
        content = hook_path.read_text(encoding='utf-8')
        if 'llmeshowyou' in content:
            print(f"  post-commit hook: installed ({hook_path})")
        else:
            print(f"  post-commit hook: exists (not ours)")
    else:
        print(f"  post-commit hook: not installed")
    return 0


# ##[Watch Mode]

def watch_files(folder: Path, recursive: bool = True, interval: int = 5, callback=None) -> None:
    """Watch folder for changes and regenerate stale maps."""
    seen_hashes: dict[str, str] = {}
    print(f"  watching {folder} (every {interval}s)...")

    while True:
        files = find_source_files([str(folder)], recursive=recursive, gitignore_root=folder)
        for src in files:
            try:
                current_hash = sha256_file(src)
                prev = seen_hashes.get(str(src))
                if prev is None:
                    seen_hashes[str(src)] = current_hash
                    continue
                if current_hash != prev:
                    seen_hashes[str(src)] = current_hash
                    out = map_path_for(src)
                    if is_stale(src, out):
                        text, lines = read_source(src)
                        fm = analyze_file(src, text, lines)
                        write_map(fm, out)
                        ts = datetime.now().strftime('%H:%M:%S')
                        print(f"  [{ts}] updated {src.name}  ({fm.lines} lines)")
                        if callback:
                            callback(fm)
            except Exception:
                continue
        # Clean up deleted files
        seen_hashes = {k: v for k, v in seen_hashes.items() if Path(k).exists()}
        time.sleep(interval)


# ##[CLI Commands]

# Sentinel injected into CLAUDE.md so we never duplicate the rule.
_CLAUDE_SENTINEL = '<!-- llmeshowyou:claude-rule -->'

CLAUDE_RULE = f"""# map.md priority
{_CLAUDE_SENTINEL}
When reading a `.map.md` file to understand a `.py` file, and additional Python files are referenced in the code or imports, first search for the corresponding `.py.map.md` files (e.g., `foo.py` \u2192 `foo.py.map.md`) and read those maps before falling back to reading the full source. Maps provide classes, functions, signatures, line ranges, imports, constants, section markers, and TODOs \u2014 use them to identify the specific lines to read from source rather than reading entire files.
"""

def _ensure_claude_md(root: Path) -> bool:
    """Inject the map.md priority rule into a project's CLAUDE.md if absent.

    Returns True if the file was modified, False if already up-to-date.
    """
    path = root / 'CLAUDE.md'
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding='utf-8')
    except Exception:
        return False
    if _CLAUDE_SENTINEL in content:
        return False
    # Prepend so it's visible immediately; add two newlines after the rule
    updated = CLAUDE_RULE + '\n' + content
    try:
        path.write_text(updated, encoding='utf-8')
        print(f"  -> added map.md priority rule to {path}")
        return True
    except Exception:
        return False

def cmd_map(args: argparse.Namespace) -> int:
    _ensure_claude_md(Path.cwd())
    ext = None if not getattr(args, 'all_langs', False) else set(LANGUAGE_EXTENSIONS.keys())
    files = find_source_files(args.files, args.recursive, extra_exts=ext)
    if not files:
        print("llmeshowyou: no source files found.", file=sys.stderr)
        return 1

    mapped: list[FileMap] = []
    errors = 0

    for src in files:
        min_l = getattr(args, 'min_lines', 0)
        if min_l and count_lines(src) < min_l:
            continue

        out = map_path_for(src)
        if args.outdir:
            out = Path(args.outdir) / out.name

        try:
            text, lines = read_source(src)
        except Exception as e:
            print(f"llmeshowyou: error reading {src}: {e}", file=sys.stderr)
            errors += 1
            continue

        try:
            fm = analyze_file(src, text, lines)
        except SyntaxError as e:
            print(f"llmeshowyou: syntax error in {src}: {e}", file=sys.stderr)
            errors += 1
            continue
        except Exception as e:
            print(f"llmeshowyou: error analyzing {src}: {e}", file=sys.stderr)
            errors += 1
            continue

        if not args.force and out.exists():
            stored_hash = read_stored_hash(out)
            if stored_hash == fm.sha256:
                print(f"  fresh  {src}  ({fm.lines} lines)")
                mapped.append(fm)
                continue

        write_map(fm, out)
        print(f"  mapped {src}  ({fm.lines} lines, {fm.total_symbols} symbols, lang={fm.language}) -> {out.name}")
        mapped.append(fm)

    if getattr(args, 'combined', False) and mapped:
        index_path = Path(args.outdir or '.') / 'INDEX.map.md'
        _write_combined_index(mapped, index_path)
        print(f"  index  -> {index_path}")

    if getattr(args, 'html', False) and mapped:
        if len(mapped) > 1:
            graph = build_project_graph(mapped)
            html_path = Path(args.outdir or '.') / 'PROJECT_GRAPH.html'
            render_html_index(graph, html_path)

    return 1 if errors == len(files) else 0

def _write_combined_index(maps: list[FileMap], path: Path) -> None:
    buf = ["# Index: Source File Maps", f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}", ""]
    for fm in maps:
        rel = Path(fm.source_path).name
        map_name = f"{rel}.map.md"
        buf.append(f"- [{rel}]({map_name}) -- {fm.lines} lines, {fm.total_symbols} symbols ({fm.language})")
    path.write_text('\n'.join(buf), encoding='utf-8')

def cmd_update(args: argparse.Namespace) -> int:
    _ensure_claude_md(Path.cwd())
    patterns: list[str] = args.files if args.files else ['.']
    ext = None if not getattr(args, 'all_langs', False) else set(LANGUAGE_EXTENSIONS.keys())
    files = find_source_files(patterns, recursive=True, extra_exts=ext)
    if not files:
        print("llmeshowyou: no source files found.", file=sys.stderr)
        return 0

    stale_count = 0
    updated_count = 0
    errors = 0
    savings_total = 0
    cost_path = Path(getattr(args, 'outdir', '.')) / 'llmeshowyou_cost.json'
    tracker = CostTracker(cost_path) if getattr(args, 'track_cost', True) else None

    for src in files:
        out = map_path_for(src)
        if not is_stale(src, out):
            continue

        stale_count += 1

        if getattr(args, 'major_only', False) and out.exists():
            text, lines = read_source(src)
            try:
                fm = analyze_file(src, text, lines)
            except (SyntaxError, Exception) as e:
                print(f"llmeshowyou: error analyzing {src}: {e}", file=sys.stderr)
                errors += 1
                continue
            if not is_major_change(src, out, fm):
                print(f"  minor  {src}  (skipped, use --force to remap)")
                continue

        if getattr(args, 'check', False):
            print(f"  stale  {src}")
            continue

        try:
            text, lines = read_source(src)
            fm = analyze_file(src, text, lines)
            write_map(fm, out)
            savings = _calc_savings(fm)
            savings_total += savings
            print(f"  update {src}  ({fm.lines} lines, {fm.total_symbols} symbols, saves ~{fmt_tokens(savings)})")
            updated_count += 1
        except (SyntaxError, Exception) as e:
            print(f"llmeshowyou: error updating {src}: {e}", file=sys.stderr)
            errors += 1

    if tracker and updated_count:
        tracker.add('update', updated_count, savings_total)

    if getattr(args, 'check', False):
        if stale_count:
            print(f"llmeshowyou: {stale_count} file(s) need updating.", file=sys.stderr)
            return 1
        print("llmeshowyou: all maps fresh.")
        return 0

    if stale_count == 0:
        print("llmeshowyou: all maps fresh. nothing to update.")
    elif updated_count == 0 and errors == 0:
        print(f"llmeshowyou: all {stale_count} stale file(s) were minor-only (skipped).")
    if savings_total and updated_count:
        print(f"  estimated tokens saved: {fmt_tokens(savings_total)}")
    return 1 if errors and updated_count == 0 else 0

# Token-cost model (see docs): ~4 chars/token is the conventional tiktoken
# heuristic for code and English alike. We deliberately use the SAME ratio for
# source and map so the comparison is apples-to-apples (an earlier version used
# 3.5 for source and 4 for the map, which understated map cost and inflated
# "savings"). REREAD_FRACTION accounts for the real workflow: a map does not
# fully replace the source -- the agent reads the map and then typically opens a
# relevant slice of the source. Net savings are therefore conservative.
CHARS_PER_TOKEN = 4.0
REREAD_FRACTION = 0.25  # assume ~25% of source still read after consulting map

def estimate_tokens(n_chars: int) -> int:
    """Estimate token count from a character count (tiktoken ~4 chars/token)."""
    return max(0, int(n_chars / CHARS_PER_TOKEN))

def _calc_savings(fm: FileMap, reread_fraction: float = REREAD_FRACTION) -> int:
    """Conservative net token savings from reading the map instead of source.

    net = src_tokens - map_tokens - reread_tokens
    where reread_tokens models the slice of source still opened after the map.
    Clamped at >= 0 so we never report a negative "saving".
    """
    src_tok = max(1, estimate_tokens(fm.size_bytes))
    try:
        map_tok = estimate_tokens(len(render_map(fm)))
    except Exception:
        # Fallback: assume ~6 chars per source line for the rendered map.
        map_tok = estimate_tokens(int(fm.lines * 6))
    reread_tok = int(src_tok * max(0.0, min(1.0, reread_fraction)))
    return max(0, src_tok - map_tok - reread_tok)

def cmd_status(args: argparse.Namespace) -> int:
    patterns: list[str] = args.dir if args.dir else ['.']
    ext = None if not getattr(args, 'all_langs', False) else set(LANGUAGE_EXTENSIONS.keys())
    files = find_source_files(patterns, getattr(args, 'recursive', False), extra_exts=ext)
    if not files:
        print("llmeshowyou: no source files found.", file=sys.stderr)
        return 0

    fresh = stale = missing = 0
    for src in files:
        out = map_path_for(src)
        if not out.exists():
            print(f"  MISSING {src}")
            missing += 1
        elif is_stale(src, out):
            print(f"  STALE   {src}")
            stale += 1
        else:
            print(f"  fresh   {src}")
            fresh += 1

    total = fresh + stale + missing
    print(f"\n{total} files: {fresh} fresh, {stale} stale, {missing} missing")
    return 1 if stale + missing else 0

def cmd_graph(args: argparse.Namespace) -> int:
    patterns: list[str] = args.dir if args.dir else ['.']
    ext = set()
    if getattr(args, 'all_langs', False):
        ext = set(LANGUAGE_EXTENSIONS.keys())
    files = find_source_files(patterns, recursive=True, extra_exts=ext)
    if not files:
        print("llmeshowyou: no source files found.", file=sys.stderr)
        return 0

    maps: list[FileMap] = []
    errors = 0
    for src in files:
        out = map_path_for(src)
        if not out.exists():
            print(f"  SKIP  {src}  (no map, run 'map' first)")
            continue
        try:
            text, lines = read_source(src)
            fm = analyze_file(src, text, lines)
            maps.append(fm)
        except (SyntaxError, Exception) as e:
            print(f"  ERROR  {src}: {e}", file=sys.stderr)
            errors += 1

    if not maps:
        print("llmeshowyou: no maps found.", file=sys.stderr)
        return 1

    graph = build_project_graph(maps)
    outdir = Path(args.outdir or '.')

    # Always print summary
    gods = rank_god_nodes(graph)
    print(f"\nProject Graph: {graph.total_nodes} nodes, {graph.total_edges} edges")
    if gods:
        print(f"\nGod Nodes (top 5):")
        for i, (fp, d) in enumerate(gods[:5], 1):
            print(f"  {i}. {Path(fp).name}  -- imported by {d} files")

    if getattr(args, 'html', False):
        html_path = outdir / 'PROJECT_GRAPH.html'
        render_html_index(graph, html_path)

    if getattr(args, 'combined', False):
        index_path = outdir / 'INDEX.map.md'
        _write_combined_index(maps, index_path)
        print(f"  index -> {index_path}")

    # Save graph JSON
    graph_json_path = outdir / 'project_graph.json'
    graph_data = {
        'nodes': [{'path': p, 'language': fm.language, 'lines': fm.lines, 'symbols': fm.total_symbols}
                  for p, fm in graph.nodes.items()],
        'edges': [{'source': e.source, 'target': e.target, 'line': e.line, 'kind': e.kind, 'name': e.name}
                  for e in graph.edges],
        'god_nodes': [{'file': Path(f).name, 'imported_by': d} for f, d in gods[:30]],
    }
    graph_json_path.write_text(json.dumps(graph_data, indent=2), encoding='utf-8')
    print(f"  graph json -> {graph_json_path}")

    return 1 if errors == len(files) else 0

def cmd_query(args: argparse.Namespace) -> int:
    # Load the most recent graph from cwd or args.dir
    dir_path = Path('.')
    graph = _load_graph(dir_path)
    if graph is None:
        print("llmeshowyou: no project graph found. Run 'graph' first.", file=sys.stderr)
        return 1
    result = query_graph(graph, args.question, mode='dfs' if args.dfs else 'bfs', budget=getattr(args, 'budget', 1000))
    print(result)
    return 0

def cmd_path(args: argparse.Namespace) -> int:
    graph = _load_graph(Path('.'))
    if graph is None:
        print("llmeshowyou: no project graph found.", file=sys.stderr)
        return 1
    print(shortest_path(graph, args.start, args.end))
    return 0

def cmd_explain(args: argparse.Namespace) -> int:
    graph = _load_graph(Path('.'))
    if graph is None:
        print("llmeshowyou: no project graph found.", file=sys.stderr)
        return 1
    print(explain_node(graph, args.node))
    return 0

def _load_graph(dir_path: Path) -> Optional[ProjectGraph]:
    graph_json = dir_path / 'project_graph.json'
    if not graph_json.exists():
        # Try parent dirs
        for p in [dir_path] + list(dir_path.parents):
            candidate = p / 'project_graph.json'
            if candidate.exists():
                graph_json = candidate
                break
        else:
            return None
    try:
        data = json.loads(graph_json.read_text(encoding='utf-8'))
    except Exception:
        return None
    g = ProjectGraph()
    for nd in data.get('nodes', []):
        g.nodes[nd['path']] = FileMap(
            source_path=nd['path'], sha256='', lines=nd.get('lines', 0),
            size_bytes=0, timestamp='', language=nd.get('language', 'unknown'),
        )
    for ed in data.get('edges', []):
        g.edges.append(DependencyEdge(**ed))
    return g

def cmd_cost(args: argparse.Namespace) -> int:
    dir_path = Path(args.dir or '.')
    cost_path = dir_path / 'llmeshowyou_cost.json'
    tracker = CostTracker(cost_path)
    if not tracker.records:
        print("llmeshowyou: no cost records found.")
        return 0
    print(f"Cost Tracker: {tracker.path}")
    print(f"Total tokens saved: {fmt_tokens(tracker.total_saved)}")
    print(f"Operations: {len(tracker.records)}")
    print(f"\nRecent operations:")
    for r in tracker.records[-10:]:
        print(f"  {r.date[:19]}  {r.operation:10s}  {r.files_processed:3d} files  saved {fmt_tokens(r.tokens_saved)}")
    return 0

def cmd_hook(args: argparse.Namespace) -> int:
    project_dir = Path(args.project_dir or '.')
    action = args.hook_action
    if action == 'install':
        return install_hook(project_dir)
    elif action == 'uninstall':
        return uninstall_hook(project_dir)
    elif action == 'status':
        return status_hook(project_dir)
    return 1

def cmd_wiki(args: argparse.Namespace) -> int:
    dir_path = Path(args.dir)
    graph = _load_graph(dir_path)
    if graph is None:
        # Build graph from scratch
        ext = set(LANGUAGE_EXTENSIONS.keys())
        files = find_source_files([str(dir_path)], recursive=True, extra_exts=ext)
        maps: list[FileMap] = []
        for src in files:
            out = map_path_for(src)
            if not out.exists():
                continue
            try:
                text, lines = read_source(src)
                maps.append(analyze_file(src, text, lines))
            except Exception:
                continue
        if not maps:
            print("llmeshowyou: no files to build wiki from.", file=sys.stderr)
            return 1
        graph = build_project_graph(maps)
    outdir = Path(args.outdir or dir_path / 'wiki')
    render_wiki(graph, outdir)
    return 0

def cmd_graphml(args: argparse.Namespace) -> int:
    graph = _load_graph(Path(args.dir or '.'))
    if graph is None:
        print("llmeshowyou: no project graph found.", file=sys.stderr)
        return 1
    out = Path(getattr(args, 'out', 'project_graph.graphml'))
    export_graphml(graph, out)
    return 0

def cmd_neo4j(args: argparse.Namespace) -> int:
    graph = _load_graph(Path(args.dir or '.'))
    if graph is None:
        print("llmeshowyou: no project graph found.", file=sys.stderr)
        return 1
    out = Path(getattr(args, 'out', 'project_graph.cypher'))
    export_neo4j_cypher(graph, out)
    return 0

def cmd_mcp(args: argparse.Namespace) -> int:
    graph = _load_graph(Path(args.dir or '.'))
    if graph is None:
        print("llmeshowyou: no project graph found. Run 'graph' first.", file=sys.stderr)
        return 1
    transport = getattr(args, 'transport', 'stdio')
    port = getattr(args, 'port', 8080)
    print(f"  MCP server starting ({transport}, port {port})...")
    mcp_serve(graph, transport=transport, port=port)
    return 0

def cmd_watch(args: argparse.Namespace) -> int:
    dir_path = Path(args.dir)
    recursive = getattr(args, 'recursive', True)
    interval = getattr(args, 'interval', 5)
    try:
        watch_files(dir_path, recursive=recursive, interval=interval)
    except KeyboardInterrupt:
        print("\n  watch stopped.")
    return 0


# ##[CLI Parser]

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='llmeshowyou',
        description='Map source files to compact markdown for LLM context efficiency -- with cross-file graph, query, export, and wiki.',
    )
    sub = p.add_subparsers(dest='command', required=True)

    # ###[map]
    map_p = sub.add_parser('map', help='Create/refresh markdown maps for source files.')
    map_p.add_argument('files', nargs='+', help='File patterns, globs, or directories.')
    map_p.add_argument('--recursive', '-r', action='store_true', help='Recurse into directories.')
    map_p.add_argument('--min-lines', type=int, default=0, help='Skip files with fewer than N lines.')
    map_p.add_argument('--combined', '-c', action='store_true', help='Write INDEX.map.md linking all maps.')
    map_p.add_argument('--html', action='store_true', help='Generate PROJECT_GRAPH.html with god nodes.')
    map_p.add_argument('--all-langs', '-a', action='store_true', help='Map non-Python files (.js, .ts, .go, .rs, ...).')
    map_p.add_argument('--outdir', '-o', default=None, help='Output directory for maps (default: same as source).')
    map_p.add_argument('--force', '-f', action='store_true', help='Force remap even if hash unchanged.')

    # ###[update]
    up_p = sub.add_parser('update', help='Remap only files whose SHA-256 has changed.')
    up_p.add_argument('files', nargs='*', default=['.'], help='Files or directories to check (default: .).')
    up_p.add_argument('--major-only', '-m', action='store_true', help='Only remap if symbols changed or line delta > 20%%.')
    up_p.add_argument('--force', '-f', action='store_true', help='Remap even if hash unchanged.')
    up_p.add_argument('--check', '-c', action='store_true', help='Check-only: exit 1 if any stale maps.')
    up_p.add_argument('--all-langs', '-a', action='store_true', help='Include non-Python files.')
    up_p.add_argument('--no-track-cost', action='store_true', help='Skip cost tracking.')

    # ###[status]
    st_p = sub.add_parser('status', help='Show which maps are fresh, stale, or missing.')
    st_p.add_argument('dir', nargs='*', default=['.'], help='Directories to check (default: .).')
    st_p.add_argument('--recursive', '-r', action='store_true', help='Recurse into subdirectories.')
    st_p.add_argument('--all-langs', '-a', action='store_true', help='Include non-Python files.')

    # ###[graph]
    gr_p = sub.add_parser('graph', help='Build cross-file import graph and rank god nodes.')
    gr_p.add_argument('dir', nargs='*', default=['.'], help='Directories to scan (default: .).')
    gr_p.add_argument('--combined', '-c', action='store_true', help='Write INDEX.map.md.')
    gr_p.add_argument('--html', action='store_true', help='Generate PROJECT_GRAPH.html visualization.')
    gr_p.add_argument('--outdir', '-o', default=None, help='Output directory.')
    gr_p.add_argument('--all-langs', '-a', action='store_true', help='Include non-Python files.')

    # ###[query]
    q_p = sub.add_parser('query', help='Search the project graph for relevant nodes.')
    q_p.add_argument('question', help='Natural-language query.')
    q_p.add_argument('--dfs', action='store_true', help='Use DFS traversal (trace one path).')
    q_p.add_argument('--budget', type=int, default=1000, help='Max result tokens.')

    # ###[path]
    pa_p = sub.add_parser('path', help='Shortest import path between two nodes.')
    pa_p.add_argument('start', help='Start node (file name or path).')
    pa_p.add_argument('end', help='End node (file name or path).')

    # ###[explain]
    ex_p = sub.add_parser('explain', help='Detailed explanation of a single node.')
    ex_p.add_argument('node', help='Node to explain (file name or path).')

    # ###[wiki]
    wi_p = sub.add_parser('wiki', help='Generate Obsidian-compatible wiki from the graph.')
    wi_p.add_argument('dir', help='Project directory.')
    wi_p.add_argument('--outdir', '-o', default=None, help='Wiki output directory (default: <dir>/wiki).')

    # ###[graphml]
    gm_p = sub.add_parser('graphml', help='Export project graph as GraphML (Gephi/yEd).')
    gm_p.add_argument('dir', nargs='?', default='.', help='Project directory.')
    gm_p.add_argument('--out', default='project_graph.graphml', help='Output file.')

    # ###[neo4j]
    ne_p = sub.add_parser('neo4j', help='Export project graph as Neo4j Cypher.')
    ne_p.add_argument('dir', nargs='?', default='.', help='Project directory.')
    ne_p.add_argument('--out', default='project_graph.cypher', help='Output file.')

    # ###[cost]
    co_p = sub.add_parser('cost', help='Show cumulative token savings from cost tracker.')
    co_p.add_argument('dir', nargs='?', default='.', help='Project directory.')

    # ###[hook]
    ho_p = sub.add_parser('hook', help='Manage git post-commit hook for auto-regeneration.')
    ho_p.add_argument('hook_action', choices=['install', 'uninstall', 'status'], help='Action.')
    ho_p.add_argument('--project-dir', default='.', help='Project root directory.')

    # ###[mcp]
    mc_p = sub.add_parser('mcp', help='Start MCP server exposing the graph (stdio or HTTP).')
    mc_p.add_argument('dir', nargs='?', default='.', help='Project directory.')
    mc_p.add_argument('--transport', choices=['stdio', 'http'], default='stdio', help='Transport.')
    mc_p.add_argument('--port', type=int, default=8080, help='HTTP port.')

    # ###[watch]
    wa_p = sub.add_parser('watch', help='Watch folder for changes and auto-regenerate maps.')
    wa_p.add_argument('dir', help='Directory to watch.')
    wa_p.add_argument('--recursive', '-r', action='store_true', help='Watch recursively.')
    wa_p.add_argument('--interval', type=int, default=5, help='Poll interval in seconds.')

    return p


# ##[main]

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    code = 0
    if args.command == 'map':
        code = cmd_map(args)
    elif args.command == 'update':
        code = cmd_update(args)
    elif args.command == 'status':
        code = cmd_status(args)
    elif args.command == 'graph':
        code = cmd_graph(args)
    elif args.command == 'query':
        code = cmd_query(args)
    elif args.command == 'path':
        code = cmd_path(args)
    elif args.command == 'explain':
        code = cmd_explain(args)
    elif args.command == 'wiki':
        code = cmd_wiki(args)
    elif args.command == 'graphml':
        code = cmd_graphml(args)
    elif args.command == 'neo4j':
        code = cmd_neo4j(args)
    elif args.command == 'cost':
        code = cmd_cost(args)
    elif args.command == 'hook':
        code = cmd_hook(args)
    elif args.command == 'mcp':
        code = cmd_mcp(args)
    elif args.command == 'watch':
        code = cmd_watch(args)
    sys.exit(code)


if __name__ == '__main__':
    main()
