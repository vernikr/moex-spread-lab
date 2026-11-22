"""Static asset resolution and the HTTP security headers served with every response."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

WEB_PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (WEB_PACKAGE_DIR / "static").resolve()
VENDOR_DIR = (WEB_PACKAGE_DIR / "vendor").resolve()

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'"
)

STATIC_MIME_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
}

INDEX_PAGES = frozenset({"/", "/proof"})
VENDOR_CHARTS = "/vendor/lightweight-charts.standalone.production.js"


def resolve_static_asset(url_path: str) -> tuple[Path, str] | None:
    """Map `/static/<rel>` to a file inside `static/`; anything else resolves to `None`."""
    rel = unquote(url_path.removeprefix("/static/"))
    if not rel or "\x00" in rel or rel.startswith("/"):
        return None
    try:
        candidate = (STATIC_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(STATIC_DIR) or not candidate.is_file():
        return None
    mime = STATIC_MIME_TYPES.get(candidate.suffix)
    if mime is None:
        return None
    return candidate, mime


def vendor_asset() -> Path:
    """The vendored charting bundle served outside `/static/`."""
    return VENDOR_DIR / "lightweight-charts.standalone.production.js"
