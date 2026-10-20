"""Build the interactive architecture map: docs/architecture/spec.yaml -> site/index.html.

    uv run python tools/build_architecture.py [--ref <commit or branch>] [--out site]

Every box in the diagram names its code by file and symbol (a dotted Python qualname such as
`build_graph.collect`, or a literal `match` string for YAML, systemd and shell files). Lines are
resolved here from the source itself, so the GitHub links and the code snippets shown in the page
never drift from the code. An unresolvable reference fails the build (and the test suite).
No network: GitHub links are just strings built from the repo name and the ref.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess  # noqa: S404 - only `git rev-parse HEAD`, fixed argv
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "architecture" / "spec.yaml"
TEMPLATE = ROOT / "docs" / "architecture" / "template.html"
MAX_SNIPPET_LINES = 60
MAX_BLOCK_LINES = 30
KINDS = {
    "entry",
    "step",
    "graph_node",
    "router",
    "subagent",
    "tool",
    "guard",
    "store",
    "external",
    "interrupt",
    "eval",
    "ops",
    "view",
}
EDGE_KINDS = {"flow", "send", "conditional", "data", "resume", "uses"}
SIDES = {"l", "r", "t", "b"}


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Located:
    start: int  # 1-indexed, inclusive
    end: int
    focus: int  # the line the reference is about (the def line, or the matched line)


def _children(node: ast.AST):
    """Statements nested in `node`, looking through if/with/try blocks but not into other defs."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Assign | ast.AnnAssign):
            yield child
        elif isinstance(child, ast.stmt):
            yield from _children(child)


def _named(node: ast.AST, name: str) -> bool:
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    return False


def locate_symbol(source: str, qualname: str) -> Located:
    node: ast.AST = ast.parse(source)
    for part in qualname.split("."):
        found = [c for c in _children(node) if _named(c, part)]
        if not found:
            raise SpecError(f"symbol {qualname!r}: no {part!r}")
        node = found[0]
    decorators = getattr(node, "decorator_list", [])
    start = min([d.lineno for d in decorators] + [node.lineno])
    return Located(start=start, end=node.end_lineno or node.lineno, focus=node.lineno)


def locate_match(lines: list[str], match: str) -> Located:
    hits = [i for i, line in enumerate(lines) if match in line]
    if len(hits) != 1:
        raise SpecError(f"match {match!r}: found {len(hits)} times, need exactly 1")
    i = hits[0]
    start = i
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = i
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    if end - start + 1 > MAX_BLOCK_LINES:  # long block: a window starting a little above the match
        start = max(start, i - 3)
        end = min(end, start + MAX_BLOCK_LINES - 1)
    return Located(start=start + 1, end=end + 1, focus=i + 1)


def resolve_ref(ref: dict, repo: str, git_ref: str, root: Path = ROOT) -> dict:
    file = ref.get("file", "")
    path = root / file
    if not file or not path.is_file():
        raise SpecError(f"file not found: {file!r}")
    text = path.read_text(encoding="utf-8")
    lines = text.replace("\r\n", "\n").split("\n")  # not splitlines(): U+2028 etc. would shift every line number
    symbol, match = ref.get("symbol") or "", ref.get("match") or ""
    if bool(symbol) == bool(match):
        raise SpecError(f"{file}: give exactly one of symbol or match")
    if symbol and not file.endswith(".py"):
        raise SpecError(f"{file}: symbol is for .py files, use match")
    loc = locate_symbol(text, symbol) if symbol else locate_match(lines, match)
    shown_end = min(loc.end, loc.start + MAX_SNIPPET_LINES - 1)
    return {
        "file": file,
        "label": ref.get("label") or symbol or match,
        "symbol": symbol,
        "start": loc.start,
        "end": loc.end,
        "focus": loc.focus,
        "code": "\n".join(lines[loc.start - 1 : shown_end]),
        "truncated": loc.end - shown_end,
        "url": f"https://github.com/{repo}/blob/{git_ref}/{file}#L{loc.start}-L{loc.end}",
    }


def _build_view(v: dict, ids: set[str], view_ids: set[str], repo: str, git_ref: str, root: Path) -> dict:
    place = v.get("place") or {}
    for nid, xy in place.items():
        if nid not in ids:
            raise SpecError(f"unknown node {nid!r}")
        if not (isinstance(xy, list) and len(xy) == 2 and all(isinstance(c, int | float) for c in xy)):
            raise SpecError(f"node {nid!r}: place must be [x, y]")
    edges = []
    for e in v.get("edges", []):
        name = f"edge {e.get('from')}->{e.get('to')}"
        for end in (e.get("from"), e.get("to")):
            if end not in place:
                raise SpecError(f"{name}: {end!r} is not placed in this view")
        if e.get("kind", "flow") not in EDGE_KINDS:
            raise SpecError(f"{name}: unknown kind {e.get('kind')!r}")
        for s in ("from_side", "to_side"):
            if s in e and e[s] not in SIDES:
                raise SpecError(f"{name}: {s} must be one of {sorted(SIDES)}")
        try:
            edges.append({**e, "ref": resolve_ref(e["ref"], repo, git_ref, root)} if e.get("ref") else dict(e))
        except SpecError as err:
            raise SpecError(f"{name}: {err}") from None
    return {**v, "groups": v.get("groups") or [], "place": place, "edges": edges}


def build_data(spec: dict, git_ref: str, root: Path = ROOT) -> dict:
    repo = spec["repo"]
    view_ids = [v["id"] for v in spec["views"]]
    if len(set(view_ids)) != len(view_ids):
        raise SpecError("duplicate view id")
    nodes, ids = [], set()
    for n in spec["nodes"]:
        nid = n["id"]
        try:
            if nid in ids:
                raise SpecError("duplicate id")
            if n.get("kind") not in KINDS:
                raise SpecError(f"unknown kind {n.get('kind')!r}")
            if n["kind"] == "view" and n.get("view") not in view_ids:
                raise SpecError(f"view node points at unknown view {n.get('view')!r}")
            refs = [resolve_ref(r, repo, git_ref, root) for r in n.get("refs", [])]
        except SpecError as e:
            raise SpecError(f"node {nid!r}: {e}") from None
        ids.add(nid)
        nodes.append({**n, "refs": refs})
    views = []
    for v in spec["views"]:
        try:
            views.append(_build_view(v, ids, set(view_ids), repo, git_ref, root))
        except SpecError as e:
            raise SpecError(f"view {v['id']!r}: {e}") from None
    placed = {nid for v in views for nid in v["place"]}
    if unplaced := sorted(ids - placed):
        raise SpecError(f"nodes not placed in any view: {', '.join(unplaced)}")
    return {
        "repo": repo,
        "ref": git_ref,
        "title": spec.get("title", "Architecture"),
        "intro": spec.get("intro", ""),
        "views": views,
        "nodes": nodes,
    }


def render(data: dict, template: str) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    if "/*__DATA__*/null" not in template:
        raise SpecError("template has no /*__DATA__*/null placeholder")
    return template.replace("/*__DATA__*/null", payload)


def _head_commit() -> str:
    try:
        argv = ["git", "rev-parse", "HEAD"]  # noqa: S607 - fixed argv, git from PATH, no shell
        out = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, check=True, timeout=10)  # noqa: S603
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "main"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_architecture")
    ap.add_argument("--ref", default=None, help="commit or branch for GitHub links (default: HEAD commit)")
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    args = ap.parse_args(argv)
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    try:
        data = build_data(spec, args.ref or _head_commit())
    except SpecError as e:
        print(f"architecture map: {e}", file=sys.stderr)
        return 1
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "index.html").write_text(render(data, TEMPLATE.read_text(encoding="utf-8")), encoding="utf-8")
    (args.out / ".nojekyll").write_text("", encoding="utf-8")
    edges = sum(len(v["edges"]) for v in data["views"])
    print(
        f"wrote {args.out / 'index.html'}: {len(data['nodes'])} components, {len(data['views'])} views, {edges} edges"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
