"""The interactive architecture map must build from the repo, with every code reference still resolving."""

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_architecture", ROOT / "tools" / "build_architecture.py")
arch = importlib.util.module_from_spec(_spec)
sys.modules["build_architecture"] = arch  # dataclasses look the module up while the class is created
_spec.loader.exec_module(arch)


def _spec_data() -> dict:
    return yaml.safe_load(arch.SPEC.read_text(encoding="utf-8"))


def test_repo_spec_builds_and_every_ref_resolves():
    data = arch.build_data(_spec_data(), "abc123")
    refs = [r for n in data["nodes"] for r in n["refs"]]
    refs += [e["ref"] for v in data["views"] for e in v["edges"] if e.get("ref")]
    assert refs
    for r in refs:
        assert r["code"].strip(), r
        assert r["url"].startswith(f"https://github.com/{data['repo']}/blob/abc123/{r['file']}#L{r['start']}")
        assert r["start"] <= r["focus"] <= r["end"]


def test_every_component_is_shown_somewhere():
    data = arch.build_data(_spec_data(), "main")
    placed = {nid for v in data["views"] for nid in v["place"]}
    assert placed == {n["id"] for n in data["nodes"]}


def test_symbol_lookup_follows_nesting_and_decorators():
    src = (
        "def build():\n    def inner():\n        pass\n    @dec\n    def hook():\n        pass\n\n"
        "class C:\n    x: int = 1\n"
    )
    assert arch.locate_symbol(src, "build.inner").start == 2
    hook = arch.locate_symbol(src, "build.hook")
    assert (hook.start, hook.focus) == (4, 5)  # snippet starts at the decorator, focus on the def
    assert arch.locate_symbol(src, "C.x").start == 9
    with pytest.raises(arch.SpecError):
        arch.locate_symbol(src, "build.missing")


def test_match_must_be_unique():
    lines = ["[Unit]", "A=1", "", "[Service]", "A=1"]
    with pytest.raises(arch.SpecError):
        arch.locate_match(lines, "A=1")
    loc = arch.locate_match(lines, "[Service]")
    assert (loc.start, loc.focus, loc.end) == (4, 4, 5)


def test_snippet_lines_match_github_numbering(tmp_path):
    # A line separator inside a string must not shift the snippet away from the AST/GitHub line numbers.
    (tmp_path / "m.py").write_text('SEP = "a b"\n\n\ndef target():\n    return 1\n', encoding="utf-8")
    ref = arch.resolve_ref({"file": "m.py", "symbol": "target"}, "o/r", "main", root=tmp_path)
    assert (ref["start"], ref["end"]) == (4, 5)
    assert ref["code"] == "def target():\n    return 1"


def test_stale_reference_fails_the_build():
    spec = _spec_data()
    spec["nodes"][0]["refs"] = [{"file": "src/job_copilot/graph.py", "symbol": "build_graph.no_such_node"}]
    with pytest.raises(arch.SpecError, match="no_such_node"):
        arch.build_data(spec, "main")


def test_edges_must_join_placed_nodes():
    spec = _spec_data()
    spec["views"][0]["edges"].append({"from": spec["nodes"][0]["id"], "to": "nowhere"})
    with pytest.raises(arch.SpecError, match="nowhere"):
        arch.build_data(spec, "main")


def test_render_cannot_break_out_of_the_script_tag():
    html = arch.render({"intro": "</script><script>alert(1)</script>"}, "<script>const D = /*__DATA__*/null;</script>")
    assert html.count("</script>") == 1
