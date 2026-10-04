"""Optional FastAPI router: admin/toggle surface for the registry.

Mount under the path the frontend already consumes, e.g. in server.py:
    from nm_skills_registry.fastapi import create_skills_router
    app.include_router(create_skills_router(registry), prefix="/api/skills")

Endpoints (all user-scoped, single user for now):
    GET  /                     → list_all() (includes enabled state)
    GET  /index                → render_index() (what the agent sees)
    POST /{name}/enabled       → body {"enabled": bool} → set_enabled
    POST /refresh              → force index rebuild
"""

from __future__ import annotations

from typing import Any

from .registry.store import SkillRegistry


class EnabledRequest:
    """body: {"enabled": bool} — pydantic model when fastapi is available."""

    def __init__(self, enabled: bool):
        self.enabled = enabled


def create_skills_router(registry: SkillRegistry):
    """Build the admin/toggle APIRouter. Requires fastapi (optional extra)."""
    try:
        from fastapi import APIRouter, HTTPException
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "nm_skills_registry.fastapi needs the 'fastapi' extra: "
            "pip install 'nm-skills-registry[fastapi]'"
        ) from e
    from pydantic import BaseModel

    class EnabledBody(BaseModel):
        enabled: bool

    router = APIRouter()

    @router.get("")
    def list_skills() -> list[dict[str, Any]]:
        return registry.list_all()

    @router.get("/index")
    def index() -> dict[str, str]:
        return {"index": registry.render_index()}

    @router.post("/{name:path}/enabled")
    def set_enabled(name: str, req: EnabledBody) -> dict[str, str]:
        result = registry.set_enabled(name, req.enabled)
        if result.startswith("Rejected"):
            raise HTTPException(status_code=404, detail=result)
        return {"status": result}

    @router.post("/refresh")
    def refresh() -> dict[str, int]:
        return {"skills": registry.refresh()}

    return router
