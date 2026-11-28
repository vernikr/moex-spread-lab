"""Eligibility signatures for consumers caching results derived from ISS history pages."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import iss_http


def history_page_is_eligible(path: Path, till_date: str | None = None) -> bool:
    """Check the same eligibility as the history page reader, without network access."""
    page = iss_http.read_history_cache_page(path)
    if page is None:
        return False
    if till_date is None:
        url = page.get("url")
        if not isinstance(url, str):
            return False
        try:
            values = parse_qs(urlsplit(url).query).get("till", [])
        except ValueError:
            return False
        if len(values) != 1:
            return False
        till_date = values[0]
    return iss_http.cache_page_is_eligible(page, till_date)


def history_eligibility_signature(directories: Iterable[Path]) -> str:
    """Hash eligibility states, including changes caused by clock skew or date rollover.

    File-content changes are still covered by each consumer's existing fingerprint.
    """
    digest = hashlib.sha256()
    for directory in directories:
        digest.update(str(directory).encode())
        digest.update(b"\0")
        try:
            files = sorted(directory.glob("*.json"))
            for path in files:
                digest.update(path.name.encode())
                digest.update(b"\1" if history_page_is_eligible(path) else b"\0")
                digest.update(b"\0")
        except OSError:
            # A directory we cannot examine must not reuse an earlier eligible snapshot.
            digest.update(b"inaccessible")
    return digest.hexdigest()
