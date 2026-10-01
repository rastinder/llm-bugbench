"""Generating benchmark tasks by mutating hermetic source.

Both organic sources were measured and are exhausted (see PLAN.md): the opencode DB yields
1 replayable triple from 163 files, and AutoPilot-Jobs yields 1 hermetic triple from 42
fix commits because its oracles drive real browsers. What remains is 259 hermetic tests
across four private codebases -- and tests alone are not tasks.

Mutation testing converts supply into tasks. Each mutant is a genuine, self-contained
defect whose ground truth is known *by construction*: if the original tests kill a mutant,
those tests are a ready-made oracle for it. That gives the panel three properties nothing
else here can offer -- an unbounded supply, a verified oracle, and a known-good patch (the
un-mutation) to calibrate the scorer against.

The mutants are deliberately shallow next to a real historical bug. They are a power and
calibration source, and the report says so plainly rather than dressing them up as organic.
"""
from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

#: Operator catalogue. Each returns a list of (lineno, col_offset, replacement_source) for
#: a single AST node, or [] when the node offers nothing to mutate.
#:
#: Deliberately excludes `keyword`/`argument` deletion on calls: deleting a call argument
#: usually raises TypeError, which a test may catch and assert on, producing a mutant that
#: is a crash rather than a behavioural defect.
NUMERIC_OPS = {
    "add": ("+", "-"),
    "sub": ("-", "+"),
    "mult": ("*", "/"),
}
COMPARISON_OPS = {
    "eq": ("==", "!="),
    "not_eq": ("!=", "=="),
    "lt": ("<", ">"),
    "gt": (">", "<"),
    "lte": ("<=", ">"),
    "gte": (">=", "<"),
}
BOOL_OPS = {"and": ("and", "or"), "or": ("or", "and")}
#: replacement symbols for AugAssign ops
AUG_OPS = {"Add": ("+", "-"), "Sub": ("-", "+"), "Mult": ("*", "/")}
CONST_OPS = {"true": (True, False), "false": (False, True), "none": (None, 0), "zero": (0, 1), "one": (1, 0)}


@dataclass(frozen=True)
class Mutant:
    """One generated defect."""

    bug_id: str
    source_file: str
    symbol: str
    operator: str
    line: int
    original: str
    mutated: str

    def as_dict(self) -> dict:
        return {
            "bug_id": self.bug_id,
            "source_file": self.source_file,
            "symbol": self.symbol,
            "operator": self.operator,
            "line": self.line,
            "original": self.original,
            "mutated": self.mutated,
        }


@dataclass
class MutantSuite:
    """All mutants for one source file, plus the edits needed to apply each."""

    source_file: str
    mutants: list[Mutant] = field(default_factory=list)

    def by_id(self) -> dict[str, Mutant]:
        return {m.bug_id: m for m in self.mutants}


def _bug_id(source_file: str, symbol: str, operator: str, line: int) -> str:
    """Stable, content-free ID.

    Stability matters: the same source must yield the same bug_id across runs, or the
    frozen manifest cannot be re-verified. The line number is part of the key because two
    operators on the same line are genuinely different defects.
    """
    raw = f"{source_file}|{symbol}|{operator}|{line}"
    return "mut_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _segments(src: str) -> list[str]:
    """Split source into lines, keeping ends, so a mutation is a pure line rewrite."""
    return src.splitlines(keepends=True)


class _Mutator(ast.NodeTransformer):
    def __init__(self, source_file: str, source_lines: list[str]):
        self.source_file = source_file
        self.source_lines = source_lines
        self.found: list[tuple[str, str, int, str, str]] = []
        self._scope: list[str] = []

    # -- scope tracking -------------------------------------------------
    def visit_FunctionDef(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()
        return node

    def visit_AsyncFunctionDef(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()
        return node

    def visit_ClassDef(self, node):
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()
        return node

    @property
    def symbol(self) -> str:
        return ".".join(self._scope) or "<module>"

    # -- operators -----------------------------------------------------
    def visit_AugAssign(self, node):
        """Augmented assignment (``x += 1``).

        This is an ``AugAssign``, not a ``BinOp``, and it is the most common arithmetic in
        accumulator-style code (``subtotal += price * qty``). Missing it silently removes
        most numeric mutants from exactly the functions worth mutating.
        """
        self.generic_visit(node)
        cls_name = type(node.op).__name__
        sym = {"Add": "+", "Sub": "-", "Mult": "*"}.get(cls_name)
        if sym:
            for repl in AUG_OPS.get(cls_name, ()):
                # Built from SOURCE TEXT, not from ast.unparse. unparse normalises quotes
                # ("price" -> 'price'), so a mutant described in unparsed form silently
                # fails to match the line it claims to mutate.
                line = _line_at(self.source_lines, node.lineno)
                if not line or sym not in line:
                    continue
                self.found.append((self.symbol, f"aug_{cls_name.lower()}", node.lineno,
                                   line.strip(), line.strip().replace(sym, repl, 1)))
        return node

    def visit_Assign(self, node):
        self.generic_visit(node)
        # A plain ``x = a + b`` is a BinOp and already covered; this visit exists so the
        # enclosing scope is tracked when a BinOp hides inside an assignment.
        return node

    def visit_BinOp(self, node):
        self.generic_visit(node)
        for name, ops in NUMERIC_OPS.items():
            if isinstance(node.op, type(getattr(__import__("ast"), {
                "add": "Add", "sub": "Sub", "mult": "Mult"}[name]))):
                for repl in ops:
                    if type(node.op).__name__.lower() == repl.lower():
                        continue
                    self.found.append((self.symbol, name, node.lineno,
                                       _unparse(node), f"{_unparse(node.left)} {repl} {_unparse(node.right)}"))
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        pairs = {
            "eq": (ast.Eq, "==", "!="), "not_eq": (ast.NotEq, "!=", "=="),
            "lt": (ast.Lt, "<", ">"), "gt": (ast.Gt, ">", "<"),
            "lte": (ast.LtE, "<=", ">"), "gte": (ast.GtE, ">=", "<"),
        }
        for i, op in enumerate(node.ops):
            for name, (cls, _orig, repl) in pairs.items():
                if isinstance(op, cls) and i == 0:
                    self.found.append((self.symbol, name, node.lineno,
                                       _unparse(node),
                                       f"{_unparse(node.left)} {repl} {_unparse(node.comparators[0])}"))
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        pairs = {"and": (ast.And, "and", "or"), "or": (ast.Or, "or", "and")}
        for name, (cls, _o, repl) in pairs.items():
            if isinstance(node.op, cls):
                vals = [self.symbol] and [_unparse(v) for v in node.values]
                self.found.append((self.symbol, name, node.lineno,
                                   _unparse(node), f" {repl} ".join(vals)))
        return node

    def visit_Constant(self, node):
        for name, (orig, repl) in CONST_OPS.items():
            if node.value is orig and isinstance(node.value, type(orig)):
                self.found.append((self.symbol, name, node.lineno,
                                   _unparse(node), _unparse(ast.Constant(value=repl))))
        return node


def _line_at(lines: list[str], lineno: int) -> str:
    """1-indexed source line, or "" when out of range."""
    if 0 < lineno <= len(lines):
        return lines[lineno - 1]
    return ""


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unparseable>"


def generate(source: str, source_file: str, limit: int | None = None) -> MutantSuite:
    """Enumerate mutants for one source file.

    Returns candidates rather than applying anything: a mutant only becomes a benchmark
    task after its oracle is confirmed to kill it, which is the admission gate's job.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return MutantSuite(source_file, [])

    mut = _Mutator(source_file, source.splitlines())
    mut.visit(tree)

    mutants: list[Mutant] = []
    seen: set[str] = set()
    for symbol, operator, line, original, mutated in mut.found:
        if original == mutated:
            continue
        bid = _bug_id(source_file, symbol, operator, line)
        if bid in seen:
            continue
        seen.add(bid)
        mutants.append(Mutant(bid, source_file, symbol, operator, line, original, mutated))
        if limit and len(mutants) >= limit:
            break

    mutants.sort(key=lambda m: (m.line, m.operator, m.symbol))
    return MutantSuite(source_file, mutants)


def apply_mutation(source: str, mutant: Mutant) -> str:
    """Return ``source`` with exactly one mutant applied.

    Applied as a whole-line rewrite keyed on the mutation's line, and verified by
    re-parsing: a mutation that produces unparseable code is a broken mutant, and is
    rejected rather than shipped.
    """
    lines = _segments(source)
    idx = mutant.line - 1
    if idx < 0 or idx >= len(lines):
        return source
    line = lines[idx]
    if mutant.original not in line:
        # The reported line moved (e.g. the operator sat on a continuation line).
        for i, cand in enumerate(lines):
            if mutant.original in cand:
                idx, line = i, cand
                break
        else:
            return source
    lines[idx] = line.replace(mutant.original, mutant.mutated, 1)
    out = "".join(lines)
    try:
        ast.parse(out)
    except SyntaxError:
        return source
    return out
