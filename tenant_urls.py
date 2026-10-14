"""Unique company portal URLs on the shared Local hostname."""
import os
import re
from typing import Optional
from urllib.parse import urljoin

DEFAULT_PORTAL_ORIGIN = "https://quanteye-ai-attendence-production.up.railway.app"


def portal_origin() -> str:
    origin = (
        os.environ.get("CLIENT_DASHBOARD_URL")
        or os.environ.get("LOCAL_DASHBOARD_URL")
        or DEFAULT_PORTAL_ORIGIN
    )
    return str(origin).rstrip("/")


def make_portal_slug(name: str, org_id: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:48]
    if not base:
        base = "org"
    suffix = re.sub(r"[^a-z0-9]+", "", (org_id or "").lower())
    slug = f"{base}-{suffix}" if suffix else base
    return slug[:80]


def company_portal_url(name: str, org_id: str, slug: Optional[str] = None) -> str:
    resolved = (slug or make_portal_slug(name, org_id)).strip("/")
    return urljoin(portal_origin() + "/", f"c/{resolved}/")
