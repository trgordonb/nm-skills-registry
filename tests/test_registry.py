"""SkillRegistry semantics — the parity contract with nm_memory_layer.SkillLibrary.

Every assertion on a return string or index format is byte-copied from the
original implementation (nm_memory_layer/skills.py). If the original ever
changes, these tests are the diff to review.
"""

from __future__ import annotations

import pytest

from nm_skills_registry import LocalDirStore, SkillRegistry
from nm_skills_registry.registry.store import _parse_frontmatter


def make_library(tmp_path, tree: dict[str, str]) -> SkillRegistry:
    root = tmp_path / "skills"
    for key, content in tree.items():
        p = root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return SkillRegistry(LocalDirStore(tmp_path), "skills/")


TREE = {
    "edgar/SKILL.md": '---\nname: edgar\ndescription: SEC filings via EdgarTools\nversion: 1.0.0\n---\n\nUse EDGAR.\n',
    "devops/deploy-k8s/SKILL.md": '---\nname: deploy-k8s\ndescription: kubectl rollout workflow\nversion: 1.0.0\n---\n\nSteps here.\n',
    "dironly/SKILL.md": "no frontmatter here\n",  # name falls back to dir name
    "browser-act/SKILL.md": '---\nname: browser-act\ndescription: stealth browser automation\n---\n\nDo browser things.\n',
}


@pytest.fixture()
def reg(tmp_path):
    return make_library(tmp_path, TREE)


def test_render_index_format(reg):
    # byte-format parity: "- name: description" lines inside <skills_index>
    idx = reg.render_index()
    assert idx == (
        "<skills_index>\n"
        "- browser-act: stealth browser automation\n"
        "- deploy-k8s: kubectl rollout workflow\n"
        "- dironly: \n"
        "- edgar: SEC filings via EdgarTools\n"
        "</skills_index>"
    )


def test_resolve_by_frontmatter_name_then_dirname(reg):
    assert reg.resolve("edgar").rel_dir == "edgar"
    assert reg.resolve("deploy-k8s").rel_dir == "devops/deploy-k8s"
    assert reg.resolve("dironly").rel_dir == "dironly"
    assert reg.resolve("missing") is None


def test_list_skills_shape(reg):
    skills = reg.list_skills()
    assert skills[0] == {
        "name": "browser-act",
        "description": "stealth browser automation",
        "path": reg.store.local_path("skills/browser-act/SKILL.md"),
    }
    assert {s["name"] for s in skills} == {"browser-act", "deploy-k8s", "dironly", "edgar"}


def test_load_skill_full_text_and_reject(reg):
    assert reg.load_skill("edgar").endswith("Use EDGAR.\n")
    assert reg.load_skill("ghost") == "Rejected: no skill named 'ghost'"


def test_create_skill_messages_and_frontmatter(reg, tmp_path):
    out = reg.create_skill("new-skill", 'Desc with "quotes"', "Body line.")
    assert out.startswith("OK: created skill 'new-skill' at ")
    text = (tmp_path / "skills" / "new-skill" / "SKILL.md").read_text()
    assert text == '---\nname: new-skill\ndescription: "Desc with \'quotes\'"\nversion: 1.0.0\n---\n\nBody line.\n'
    assert "Rejected: skill 'new-skill' already exists; use patch or edit" == reg.create_skill(
        "new-skill", "d", "b"
    )
    assert "Rejected: skill body is empty" in reg.create_skill("other", "d", "  ")
    assert "Rejected: skill name must match" in reg.create_skill("Bad Name", "d", "b")
    assert "Rejected: category must be a slug" in reg.create_skill("ok", "d", "b", category="Bad Cat")


def test_create_with_category_nests(reg, tmp_path):
    reg.create_skill("deploy", "d", "b", category="devops")
    assert (tmp_path / "skills" / "devops" / "deploy" / "SKILL.md").is_file()
    assert reg.resolve("deploy").rel_dir == "devops/deploy"


def test_patch_semantics(reg):
    assert reg.patch_skill("edgar", "EDGAR", "SEC EDGAR") == "OK: patched 'edgar'"
    assert "SEC EDGAR" in reg.load_skill("edgar")
    assert "Rejected: old_text not found in edgar's SKILL.md" == reg.patch_skill(
        "edgar", "nope", "x"
    )
    assert "Rejected: no skill named 'ghost'" == reg.patch_skill("ghost", "a", "b")


def test_edit_keeps_frontmatter(reg):
    assert reg.edit_skill("edgar", "New body.") == "OK: rewrote body of 'edgar'"
    text = reg.load_skill("edgar")
    assert text.startswith("---\nname: edgar\ndescription: \"SEC filings via EdgarTools\"")
    assert text.endswith("New body.\n")
    assert reg.edit_skill("edgar", "   ") == "Rejected: skill body is empty"


def test_delete_skill_removes_tree(reg):
    assert reg.delete_skill("deploy-k8s") == "OK: deleted skill 'deploy-k8s'"
    assert reg.resolve("deploy-k8s") is None
    assert not any(k.startswith("skills/devops/") for k in
                   [m.key for m in reg.store.list_prefix("skills/")])
    assert reg.delete_skill("deploy-k8s") == "Rejected: no skill named 'deploy-k8s'"


def test_write_and_remove_skill_file(reg):
    assert reg.write_skill_file("edgar", "references/notes.md", "n1") == \
        "OK: wrote 'references/notes.md' in 'edgar'"
    assert reg.store.get_text("skills/edgar/references/notes.md") == "n1"
    assert reg.remove_skill_file("edgar", "references/notes.md") == \
        "OK: removed 'references/notes.md' from 'edgar'"
    assert "Rejected: 'references/notes.md' not found in 'edgar'" == \
        reg.remove_skill_file("edgar", "references/notes.md")


@pytest.mark.parametrize("bad", ["/abs/x.md", "../x.md", "a/../../b.md", ""])
def test_guarded_paths_reject_traversal(reg, bad):
    assert f"Rejected: invalid relative path {bad!r}" == reg.write_skill_file("edgar", bad, "x")


def test_enable_disable_visibility(reg):
    assert reg.set_enabled("edgar", False) == "OK: disabled skill 'edgar'"
    # invisible to the agent surface
    assert reg.resolve("edgar") is None
    assert reg.load_skill("edgar") == "Rejected: no skill named 'edgar'"
    names = {s["name"] for s in reg.list_skills()}
    assert "edgar" not in names
    # still visible to the admin surface
    all_names = {r["name"]: r["enabled"] for r in reg.list_all()}
    assert all_names["edgar"] is False
    assert reg.set_enabled("edgar", True) == "OK: enabled skill 'edgar'"
    assert reg.resolve("edgar") is not None
    # disabling by rel path also works
    assert reg.set_enabled("devops/deploy-k8s", False).startswith("OK: disabled")


def test_state_survives_registry_reconstruction(tmp_path):
    reg1 = make_library(tmp_path, {"x/SKILL.md": "---\nname: x\ndescription: d\n---\n\nb\n"})
    reg1.set_enabled("x", False)
    reg2 = make_library(tmp_path, {})  # empty tree, same store/state key
    reg2.refresh()
    assert reg2.state.is_disabled("x")


def test_cas_conflict_on_patch_resolves(reg):
    entry = reg.resolve("edgar")
    # stomp the file behind the registry's back with a different etag
    reg.store.put(entry.key, b"---\nname: edgar\ndescription: d\n---\n\nstomped\n")
    reg.patch_skill("edgar", "EDGAR", "EDGAR2")  # must CAS-retry, not corrupt
    text = reg.load_skill("edgar")
    assert "stomped" in text  # retry re-read the stomped version


def test_frontmatter_parity():
    # exact port of the original parser's edge cases
    assert _parse_frontmatter("no fm") == ({}, "no fm")
    assert _parse_frontmatter("---\nbroken") == ({}, "---\nbroken")
    fm, body = _parse_frontmatter("---\nname: x\n---\n\nbody\n")
    assert fm == {"name": "x"} and body == "body\n"
    fm, body = _parse_frontmatter("---\n: : [\n---\n\nbody\n")
    assert fm == {} and body == "body\n"
