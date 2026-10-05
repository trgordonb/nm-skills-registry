"""``nm-skills`` CLI — import, pull, push, sync, list, enable/disable, doctor.

All commands read ``SKILLS_REGISTRY`` (+ ``R2_*``/``AWS_*`` credentials) from
the environment or ``./.env`` in the current directory.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .env import load_env_file, registry_from_env, store_from_url
from .registry.store import SkillRegistry
from .storage.cache import CachedStore
from .storage.local import LocalDirStore

SKIP_DIRS = {"__pycache__", ".git", ".pytest_cache", ".venv"}
SKIP_SUFFIXES = (".pyc",)


def _local_md5(full: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with full.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _local_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fn.endswith(SKIP_SUFFIXES):
                continue
            full = Path(dirpath) / fn
            yield full.relative_to(root).as_posix(), full


def _resolve_url(args) -> str:
    if getattr(args, "url", None):
        return args.url
    load_env_file("./.env")
    url = os.environ.get("SKILLS_REGISTRY")
    if not url:
        sys.exit("error: SKILLS_REGISTRY not set (pass --url or set it in ./.env)")
    return url


def _authority_and_prefix(args, local_dir: Path):
    url = _resolve_url(args)
    store, explicit_prefix = store_from_url(url)
    prefix = explicit_prefix or (local_dir.name + "/")
    return url, store, prefix


def _store_and_registry(args, local_dir: Path):
    url, authority, prefix = _authority_and_prefix(args, local_dir)
    cached = CachedStore(authority, local_dir.parent)
    registry = SkillRegistry(cached, prefix)
    return url, cached, registry, prefix


# -- commands ---------------------------------------------------------------


def cmd_import(args) -> int:
    local_dir = Path(args.dir)
    if not local_dir.is_dir():
        sys.exit(f"error: {local_dir} is not a directory")
    url, authority, prefix = _authority_and_prefix(args, local_dir)
    count = 0
    for key, full in _local_files(local_dir):
        authority.put(prefix + key, full.read_bytes())
        count += 1
    print(f"imported {count} objects from {local_dir}/ into {url} under {prefix!r}")
    return 0


def cmd_pull(args) -> int:
    local_dir = Path(args.dir)
    url, cached, registry, prefix = _store_and_registry(args, local_dir)
    report = cached.sync(prefix)
    print(
        f"pull from {url} {prefix!r} → {local_dir}/: "
        f"{len(report.pulled)} pulled, {len(report.removed)} removed, {report.unchanged} unchanged"
    )
    return 0


def cmd_push(args) -> int:
    local_dir = Path(args.dir)
    if not local_dir.is_dir():
        sys.exit(f"error: {local_dir} is not a directory")
    url, authority, prefix = _authority_and_prefix(args, local_dir)
    uploaded = removed = 0
    authority_metas = {m.key: m for m in authority.list_prefix(prefix)}
    for key, full in _local_files(local_dir):
        full_key = prefix + key
        meta = authority_metas.get(full_key)
        if meta is not None and meta.etag and meta.etag.strip('"') == _local_md5(full):
            continue  # R2 etags are content md5s for simple puts → exact change detection
        authority.put(full_key, full.read_bytes())
        uploaded += 1
    if args.prune:
        local_keys = {prefix + k for k, _ in _local_files(local_dir)}
        for full_key in authority_metas:
            if full_key not in local_keys:
                authority.delete(full_key)
                removed += 1
    print(f"push {local_dir}/ → {url} {prefix!r}: {uploaded} uploaded, {removed} pruned")
    return 0


def cmd_sync(args) -> int:
    rc = cmd_pull(args)
    if rc != 0:
        return rc
    return cmd_push(args)


def cmd_list(args) -> int:
    load_env_file("./.env")
    skills_dir = os.environ.get("SKILLS_DIR", "skills")
    registry = registry_from_env(skills_dir)
    registry.refresh()
    rows = registry.list_all()
    if not rows:
        print("(no skills)")
        return 0
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        flag = "✓" if r["enabled"] else "✗"
        line = f" {flag} {r['name']:<{width}}  {r['description']}"
        print(line)
    return 0


def cmd_toggle(args) -> int:
    load_env_file("./.env")
    skills_dir = os.environ.get("SKILLS_DIR", "skills")
    registry = registry_from_env(skills_dir)
    registry.refresh()
    print(registry.set_enabled(args.name, args.command == "enable"))
    return 0


def cmd_doctor(args) -> int:
    load_env_file("./.env")
    url = os.environ.get("SKILLS_REGISTRY", "(unset — local skills dir only)")
    print(f"SKILLS_REGISTRY: {url}")
    skills_dir = Path(os.environ.get("SKILLS_DIR", "skills"))
    registry = registry_from_env(skills_dir)
    try:
        n = registry.refresh()
    except Exception as e:
        print(f"FAIL: registry unreachable: {type(e).__name__}: {e}")
        print("      offline mode: the mirror under {}/ still serves reads".format(skills_dir))
        return 1
    print(f"registry OK: {n} skills indexed; user={registry.user_id}")
    disabled = registry.state.disabled()
    print(f"disabled ({len(disabled)}): {', '.join(disabled) or '(none)'}")
    store = registry.store
    if isinstance(store, CachedStore):
        prefix = registry.prefix
        drift = store.drift(prefix)
        print(f"mirror: {skills_dir}/ — drift: {len(drift)} file(s) modified outside the registry")
        for key in drift[:10]:
            print(f"  ~ {key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nm-skills", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_: str, *, fn, needs_dir=False, needs_name=False):
        p = sub.add_parser(name, help=help_)
        if needs_dir:
            p.add_argument("dir", help="local directory")
        if needs_name:
            p.add_argument("name", help="skill name (frontmatter name or dir slug)")
        p.add_argument("--url", help="registry URL (default: SKILLS_REGISTRY)")
        p.set_defaults(fn=fn)
        return p

    add("import", "upload a local directory into the registry", fn=cmd_import, needs_dir=True)
    add("pull", "pull the registry into the local mirror", fn=cmd_pull, needs_dir=True)
    p_push = add("push", "push new/changed local files into the registry", fn=cmd_push, needs_dir=True)
    p_push.add_argument("--prune", action="store_true",
                        help="also delete registry keys that no longer exist locally")
    add("sync", "pull then push", fn=cmd_sync, needs_dir=True)
    add("list", "list skills with enabled state", fn=cmd_list)
    add("enable", "enable a skill", fn=cmd_toggle, needs_name=True)
    add("disable", "disable a skill", fn=cmd_toggle, needs_name=True)
    add("doctor", "connectivity + mirror health", fn=cmd_doctor)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
