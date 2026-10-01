"""Every text-mode file read or write in tools/ and tools/tests names its encoding.

Without `encoding=`, Python decodes with the locale's code page (cp949 on a Korean
Windows host), and CI cannot see it: every job sets PYTHONUTF8=1. So this reads the source.

Checked: Path.read_text/write_text, builtin/io open, os.fdopen and Path.open, unless the
mode is binary; `<module>.open` (tarfile.open, ...) has another signature and is skipped.
Text-mode subprocess calls decode a child's output with the locale too, so each must name
`encoding=` and an explicit `errors=`.
"""
import ast
import unittest
from support import TOOLS


def _mode(node: ast.Call, position: int) -> ast.expr | None:
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return keyword.value
    return node.args[position] if len(node.args) > position else None


def unencoded_text_io(source: str, filename: str = "<source>") -> tuple[list[int], int]:
    """Lines of text-mode file I/O calls without `encoding=`, and the call count."""
    tree = ast.parse(source, filename)
    modules = {alias.asname or alias.name.split(".")[0]
               for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names}
    flagged, checked = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ("read_text", "write_text"):
            mode = None
        elif isinstance(func, ast.Name) and func.id == "open":
            mode = _mode(node, 1)
        elif isinstance(func, ast.Attribute) and func.attr in ("open", "fdopen"):
            owner = func.value.id if isinstance(func.value, ast.Name) else None
            if func.attr == "fdopen" or owner == "io":
                mode = _mode(node, 1)
            elif owner in modules:
                continue
            else:
                mode = _mode(node, 0)
        else:
            continue
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value:
            continue
        checked += 1
        # `**kwargs` could carry an encoding, but nothing here can see it.
        if not any(keyword.arg == "encoding" for keyword in node.keywords):
            flagged.append(node.lineno)
    return flagged, checked


#: The subprocess functions that decode a child's output in text mode.
SUBPROCESS_CALLS = {"run", "check_output", "Popen", "call", "check_call"}


def undecided_subprocess_text(source: str, filename: str = "<source>") -> tuple[list[int], int]:
    """Lines of text-mode subprocess calls missing `encoding=` or `errors=`,
    and the text-mode call count. Text mode is `text=` or `universal_newlines=`
    set to anything but a literal false, or `encoding=` / `errors=` alone."""
    tree = ast.parse(source, filename)
    modules = {alias.asname or alias.name
               for node in ast.walk(tree) if isinstance(node, ast.Import)
               for alias in node.names if alias.name == "subprocess"}
    functions = {alias.asname or alias.name
                 for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module == "subprocess"
                 for alias in node.names if alias.name in SUBPROCESS_CALLS}
    flagged, checked = [], 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_module_call = (isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_CALLS
                          and isinstance(func.value, ast.Name) and func.value.id in modules)
        if not (is_module_call or (isinstance(func, ast.Name) and func.id in functions)):
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        text = any(
            name in keywords
            and not (isinstance(keywords[name], ast.Constant) and not keywords[name].value)
            for name in ("text", "universal_newlines"))
        if not (text or "encoding" in keywords or "errors" in keywords):
            continue
        checked += 1
        # `**kwargs` could carry them, but nothing here can see it.
        if "encoding" not in keywords or "errors" not in keywords:
            flagged.append(node.lineno)
    return flagged, checked


#: (folder, scanner, a file its glob must find, floor on the calls it checks). The tests
#: run the tools as children and read their output, so they are held to the same rule.
SCANS = [
    (TOOLS, unencoded_text_io, "extract_healing_observations.py", 50),
    (TOOLS, undecided_subprocess_text, "verify_build_corpus.py", 5),
    (TOOLS / "tests", unencoded_text_io, "test_check_ascii.py", 100),
    (TOOLS / "tests", undecided_subprocess_text, "test_check_ascii.py", 10),
]


class TextIoEncodingTests(unittest.TestCase):
    def test_every_text_mode_call_names_its_encoding(self):
        sources = {folder: {path.name: path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.py"))}
                   for folder in (TOOLS, TOOLS / "tests")}
        for folder, scanner, sentinel, floor in SCANS:
            with self.subTest(folder=folder.name, scanner=scanner.__name__):
                # A glob that found nothing would pass vacuously.
                self.assertIn(sentinel, sources[folder])
                flagged, checked = [], 0
                for name, source in sources[folder].items():
                    lines, count = scanner(source, name)
                    flagged += [f"{name}:{line}" for line in lines]
                    checked += count
                self.assertGreater(checked, floor)
                self.assertEqual(flagged, [], "text-mode I/O without encoding= (and, for a "
                                 "subprocess, errors=) decodes with the locale")

    def test_the_subprocess_scanner_flags_each_undecided_shape(self):
        cases = {
            "import subprocess\nsubprocess.run(c, text=True)": 1,
            "import subprocess\nsubprocess.run(c, universal_newlines=True)": 1,
            "import subprocess\nsubprocess.check_output(c, text=flag)": 1,
            "import subprocess\nsubprocess.run(c, text=True, encoding='utf-8')": 1,
            "import subprocess\nsubprocess.run(c, errors='replace')": 1,
            "import subprocess as sp\nsp.Popen(c, text=True)": 1,
            "from subprocess import check_output\ncheck_output(c, text=True)": 1,
            "import subprocess\nsubprocess.run(c, text=True, encoding='utf-8', errors='strict')": 0,
            "import subprocess\nsubprocess.run(c, capture_output=True)": 0,
            "import subprocess\nsubprocess.run(c, text=False)": 0,
            "runner.run(suite, text=True)": 0,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                flagged, _ = undecided_subprocess_text(source)
                self.assertEqual(len(flagged), expected)

    def test_the_scanner_flags_each_unencoded_shape(self):
        cases = {
            "p.read_text()": 1,
            "p.write_text(s)": 1,
            "open(name)": 1,
            "open(name, 'w')": 1,
            "p.open()": 1,
            "p.open(mode='a')": 1,
            "os.fdopen(fd, 'w')": 1,
            "io.open(name, 'r')": 1,
            "p.read_text(encoding='utf-8')": 0,
            "p.write_text(s, encoding='utf-8', newline='\\n')": 0,
            "open(name, 'rb')": 0,
            "p.open('xb')": 0,
            "os.fdopen(fd, 'w', encoding=e)": 0,
            "import tarfile\ntarfile.open(fileobj=f)": 0,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                flagged, _ = unencoded_text_io(source)
                self.assertEqual(len(flagged), expected)
