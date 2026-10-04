"""SkillRegistry: the skills library over object storage.

A byte-level port of ``nm_memory_layer.skills.SkillLibrary`` semantics (error
strings, frontmatter template, resolve ordering, return messages) over an
ObjectStore, plus per-user enable/disable. The parity contract lives in the
tests: ``render_index``/``list_skills``/``resolve``/CRUD outputs must match
the original implementation against the same tree.

Disabled skills are *invisible* to the agent-facing surface (resolve, load,
index) — the same effect as not existing — and remain visible to the
admin/toggle surface (``list_all``, ``set_enabled``, CLI, REST).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from ..storage.base import AlreadyExistsError, ObjectStore, PreconditionFailedError
from .state import SkillState

_SKILL_FILE = "SKILL.md"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into (frontmatter dict, body). Missing/malformed frontmatter -> ({}, full text)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}, parts[2].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")


class _Reject(Exception):
    """Raised inside an _rmw mutation to return a rejection message."""


@dataclass
class SkillEntry:
    rel_dir: str  # posix path under the prefix, e.g. "devops/deploy-k8s"
    key: str  # full store key of the SKILL.md
    name: str
    description: str
    etag: str | None


class SkillRegistry:
    def __init__(
        self,
        store: ObjectStore,
        prefix: str = "skills/",
        *,
        user_id: str = "default",
        state: SkillState | None = None,
    ):
        self.store = store
        self.prefix = prefix if not prefix or prefix.endswith("/") else prefix + "/"
        self.user_id = user_id
        self.state = state or SkillState(store, user_id)
        self._index: dict[str, SkillEntry] | None = None
        self.last_sync_error: str | None = None

    # -- index ---------------------------------------------------------------

    def refresh(self) -> int:
        """Rebuild the in-memory index. Backends with a ``sync`` (CachedStore)
        reconcile the mirror first, so this costs one list + conditional GETs.
        If the authority is unreachable, the mirror's last-known content is
        served (offline mode) and the error is recorded for ``doctor``."""
        sync = getattr(self.store, "sync", None)
        if callable(sync):
            try:
                sync(self.prefix)
            except Exception as e:  # offline: keep serving the mirror
                self.last_sync_error = f"{type(e).__name__}: {e}"
        index: dict[str, SkillEntry] = {}
        for meta in self.store.list_prefix(self.prefix):
            if not meta.key.endswith("/" + _SKILL_FILE):
                continue
            rel_dir = meta.key[len(self.prefix) : -len("/" + _SKILL_FILE)]
            try:
                text = self.store.get_text(meta.key)
            except KeyError:
                continue  # deleted mid-scan
            fm, _ = _parse_frontmatter(text)
            index[rel_dir] = SkillEntry(
                rel_dir=rel_dir,
                key=meta.key,
                name=str(fm.get("name") or rel_dir.rsplit("/", 1)[-1]),
                description=str(fm.get("description") or ""),
                etag=meta.etag,
            )
        self._index = index
        return len(index)

    def _entries(self) -> dict[str, SkillEntry]:
        if self._index is None:
            self.refresh()
        assert self._index is not None
        return self._index

    def _update_entry(self, key: str) -> None:
        """Re-read one SKILL.md into the index after a write."""
        rel_dir = key[len(self.prefix) : -len("/" + _SKILL_FILE)]
        try:
            text, etag = self.store.get_with_etag(key)
        except KeyError:
            self._entries().pop(rel_dir, None)
            return
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        fm, _ = _parse_frontmatter(text)
        self._entries()[rel_dir] = SkillEntry(
            rel_dir=rel_dir,
            key=key,
            name=str(fm.get("name") or rel_dir.rsplit("/", 1)[-1]),
            description=str(fm.get("description") or ""),
            etag=etag,
        )

    # -- resolution ----------------------------------------------------------

    def resolve(self, name: str) -> SkillEntry | None:
        """Find an *enabled* skill by frontmatter name or directory name.

        Iterates in key order, matching the original implementation's sorted
        rglob scan (pathlib Path sort == full-string sort)."""
        for entry in self._entries().values():
            if self.state.is_disabled(entry.rel_dir):
                continue
            if entry.name == name or entry.rel_dir.rsplit("/", 1)[-1] == name:
                return entry
        return None

    # -- listing / loading ---------------------------------------------------

    def list_skills(self) -> list[dict]:
        """Enabled skills as [{name, description, path}] — the agent-facing view."""
        out = []
        for entry in self._entries().values():
            if self.state.is_disabled(entry.rel_dir):
                continue
            out.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "path": self.store.local_path(entry.key),
                }
            )
        return out

    def list_all(self) -> list[dict]:
        """Admin view: every skill plus its enabled state."""
        out = []
        for entry in self._entries().values():
            out.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "path": self.store.local_path(entry.key),
                    "rel_dir": entry.rel_dir,
                    "enabled": not self.state.is_disabled(entry.rel_dir),
                }
            )
        return out

    def render_index(self) -> str:
        """System-prompt block: names + descriptions ONLY (progressive disclosure)."""
        skills = self.list_skills()
        if not skills:
            return ""
        lines = [f"- {s['name']}: {s['description']}" for s in skills]
        return "<skills_index>\n" + "\n".join(lines) + "\n</skills_index>"

    def load_skill(self, name: str) -> str:
        """Full SKILL.md content — the progressive-disclosure second step."""
        entry = self.resolve(name)
        if entry is None:
            return f"Rejected: no skill named {name!r}"
        return self.store.get_text(entry.key)

    # -- curation ------------------------------------------------------------

    @staticmethod
    def _frontmatter(name: str, description: str) -> str:
        safe_description = description.replace('"', "'")
        return f"---\nname: {name}\ndescription: \"{safe_description}\"\nversion: 1.0.0\n---\n\n"

    def _skill_key(self, name: str, category: str | None = None) -> str:
        base = f"{self.prefix}{category}/" if category else self.prefix
        return f"{base}{name}/{_SKILL_FILE}"

    def create_skill(
        self, name: str, description: str, content: str, category: str | None = None
    ) -> str:
        if not _NAME_RE.match(name):
            return f"Rejected: skill name must match {_NAME_RE.pattern!r} (got {name!r})"
        if not _NAME_RE.match(category or "x"):
            return f"Rejected: category must be a slug (got {category!r})"
        key = self._skill_key(name, category)
        if self.store.exists(key):
            return f"Rejected: skill {name!r} already exists; use patch or edit"
        if not content.strip():
            return "Rejected: skill body is empty"
        try:
            self.store.put(
                key,
                (self._frontmatter(name, description) + content.strip() + "\n").encode("utf-8"),
                create=True,
            )
        except AlreadyExistsError:
            return f"Rejected: skill {name!r} already exists; use patch or edit"
        self._update_entry(key)
        return f"OK: created skill {name!r} at {self.store.local_path(key)}"

    def _rmw(self, name: str, mutate) -> None:
        """Read-modify-write on a SKILL.md with CAS and bounded retry. ``mutate``
        returns the new text or raises :class:`_Reject`."""
        for _ in range(3):
            entry = self.resolve(name)
            if entry is None:
                raise _Reject(f"Rejected: no skill named {name!r}")
            text, etag = self.store.get_with_etag(entry.key)
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            new_text = mutate(entry, text)
            try:
                self.store.put(entry.key, new_text.encode("utf-8"), if_match=etag)
            except PreconditionFailedError:
                self.refresh()
                continue
            self._update_entry(entry.key)
            return
        raise PreconditionFailedError(f"could not update {name!r} after 3 attempts")

    def patch_skill(self, name: str, old_text: str, new_text: str) -> str:
        """Targeted update (the preferred action): replace one exact string."""

        def mutate(entry, text):
            if old_text not in text:
                raise _Reject(f"Rejected: old_text not found in {name}'s SKILL.md")
            return text.replace(old_text, new_text, 1)

        try:
            self._rmw(name, mutate)
        except _Reject as r:
            return str(r)
        return f"OK: patched {name!r}"

    def edit_skill(self, name: str, content: str) -> str:
        """Full body rewrite (keep frontmatter). Discouraged vs patch."""
        if not content.strip():
            return "Rejected: skill body is empty"
        try:
            entry = self.resolve(name)
            if entry is None:
                raise _Reject(f"Rejected: no skill named {name!r}")
            meta, _ = _parse_frontmatter(self.store.get_text(entry.key))

            def mutate(_entry, _text):
                return self._frontmatter(
                    str(meta.get("name") or entry.rel_dir.rsplit("/", 1)[-1]),
                    str(meta.get("description") or ""),
                ) + content.strip() + "\n"

            self._rmw(name, mutate)
        except _Reject as r:
            return str(r)
        return f"OK: rewrote body of {name!r}"

    def delete_skill(self, name: str) -> str:
        entry = self.resolve(name)
        if entry is None:
            return f"Rejected: no skill named {name!r}"
        self.store.delete_prefix(f"{self.prefix}{entry.rel_dir}/")
        self._entries().pop(entry.rel_dir, None)
        return f"OK: deleted skill {name!r}"

    def _guarded_key(self, name: str, relative_path: str) -> str | None:
        entry = self.resolve(name)
        if entry is None:
            return None
        rel = relative_path
        if not rel or rel.startswith("/") or "\\" in rel or ".." in rel.split("/") or rel.endswith("/"):
            return None
        return entry.key.rsplit("/", 1)[0] + "/" + rel

    def write_skill_file(self, name: str, relative_path: str, content: str) -> str:
        key = self._guarded_key(name, relative_path)
        if key is None:
            if self.resolve(name) is None:
                return f"Rejected: no skill named {name!r}"
            return f"Rejected: invalid relative path {relative_path!r}"
        self.store.put(key, content.encode("utf-8"))
        return f"OK: wrote {relative_path!r} in {name!r}"

    def remove_skill_file(self, name: str, relative_path: str) -> str:
        key = self._guarded_key(name, relative_path)
        if key is None:
            if self.resolve(name) is None:
                return f"Rejected: no skill named {name!r}"
            return f"Rejected: invalid relative path {relative_path!r}"
        if not self.store.exists(key):
            return f"Rejected: {relative_path!r} not found in {name!r}"
        self.store.delete(key)
        return f"OK: removed {relative_path!r} from {name!r}"

    # -- toggles -------------------------------------------------------------

    def set_enabled(self, name_or_rel: str, enabled: bool) -> str:
        """Enable/disable a skill by name or rel path. Returns a status message."""
        rel_dir: str | None = None
        if name_or_rel in self._entries():
            rel_dir = name_or_rel
        else:
            for entry in self._entries().values():
                if entry.name == name_or_rel or entry.rel_dir.rsplit("/", 1)[-1] == name_or_rel:
                    rel_dir = entry.rel_dir
                    break
        if rel_dir is None:
            return f"Rejected: no skill named {name_or_rel!r}"
        self.state.set_disabled(rel_dir, not enabled)
        return f"OK: {'enabled' if enabled else 'disabled'} skill {rel_dir!r}"
