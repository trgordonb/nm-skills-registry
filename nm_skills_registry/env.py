"""Environment plumbing: URL parsing, .env loading, registry construction.

``SKILLS_REGISTRY`` selects the backend:
- ``local:<path>``           — LocalDirStore rooted at <path> (dev/tests)
- ``s3://<bucket>[/<prefix>]`` — S3-compatible store; credentials/endpoint from
  ``R2_*`` or ``AWS_*`` env vars (or .env), mirror = parent of the skills dir.

``SKILLS_USER_ID`` selects the toggle profile (default ``"default"``).
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .registry.store import SkillRegistry
from .storage.base import ObjectStore
from .storage.cache import CachedStore
from .storage.local import LocalDirStore
from .storage.s3 import S3ObjectStore


def load_env_file(path: str | Path, override: bool = False) -> bool:
    """Parse KEY=VALUE lines into os.environ (no shell expansion, no export)."""
    p = Path(path)
    if not p.is_file():
        return False
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value
    return True


def _ensure_env_loaded() -> None:
    if os.environ.get("SKILLS_REGISTRY"):
        return
    for candidate in (os.environ.get("SKILLS_REGISTRY_ENV_FILE"), "./.env"):
        if candidate and load_env_file(candidate):
            break


def store_from_url(url: str) -> tuple[ObjectStore, str]:
    """Parse a registry URL → (store, prefix)."""
    if url.startswith("local:"):
        return LocalDirStore(url[len("local:") :]), ""
    parsed = urlparse(url)
    if parsed.scheme not in ("s3", "r2"):
        raise ValueError(f"unsupported registry URL: {url!r} (use local: or s3://)")
    bucket = parsed.netloc
    if not bucket:
        raise ValueError(f"registry URL missing bucket: {url!r}")
    store = S3ObjectStore.from_env() if not _explicit_creds() else S3ObjectStore.from_config(
        bucket,
        endpoint=os.environ.get("R2_ENDPOINT_URL") or os.environ.get("AWS_ENDPOINT_URL"),
        access_key_id=os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID"),
        secret_access_key=os.environ.get("R2_SECRET_ACCESS_KEY")
        or os.environ.get("AWS_SECRET_ACCESS_KEY"),
        region=os.environ.get("R2_REGION") or os.environ.get("AWS_REGION") or "auto",
    )
    prefix = parsed.path.strip("/")
    return store, prefix


def _explicit_creds() -> bool:
    return bool(
        os.environ.get("R2_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY_ID")
    )


def registry_from_env(
    skills_dir: str | Path = "skills",
    *,
    url: str | None = None,
) -> SkillRegistry:
    """Build a SkillRegistry from SKILLS_REGISTRY (env or explicit url).

    The mirror root is the *parent* of ``skills_dir`` so full keys like
    ``skills/<cat>/<name>/SKILL.md`` materialize at exactly the local paths
    the agent's shell tools already use. For ``local:`` URLs the store IS the
    skills dir (no mirror, no cache — the plain filesystem is the authority)."""
    _ensure_env_loaded()
    url = url or os.environ.get("SKILLS_REGISTRY")
    skills_dir = Path(skills_dir or os.getenv("SKILLS_DIR", "skills"))
    user_id = os.environ.get("SKILLS_USER_ID", "default")

    if url is None:
        # No registry configured: the local skills dir is the authority.
        return SkillRegistry(LocalDirStore(skills_dir), "", user_id=user_id)

    if url.startswith("local:"):
        # bucket-root layout: the store root holds skills/ and state/ siblings
        store = LocalDirStore(url[len("local:") :])
        return SkillRegistry(store, skills_dir.name + "/", user_id=user_id)

    store, explicit_prefix = store_from_url(url)
    # Default prefix = the skills dir's own name, so bucket layout mirrors
    # local layout 1:1 (SKILLS_REGISTRY=s3://bucket + SKILLS_DIR=skills → skills/).
    prefix = explicit_prefix or (skills_dir.name + "/")
    mirror = skills_dir.parent if str(skills_dir.parent) else Path(".")
    cached = CachedStore(store, mirror)
    return SkillRegistry(cached, prefix, user_id=user_id)
