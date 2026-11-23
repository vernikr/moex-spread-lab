"""Synthetic research fixture validation and W1-W2 wave runners."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from moex_spread_lab.core.w0 import W0Error, load_json

RESEARCH_FIXTURE_SCHEMA = "research.fixture.v1"
RESEARCH_REPORT_SCHEMA = "research.report.v1"

_REQUIRED_FIXTURE_SECTIONS = (
    "catalog",
    "prices",
    "corporate_actions",
    "contracts",
    "spread_series",
    "execution",
    "portfolio",
    "strategy",
    "validation",
)
_REQUIRED_PRICE_ROLES = frozenset(
    {
        "settlement",
        "analytical_close",
        "last_trade_close",
        "bid",
        "ask",
        "live_mark",
    }
)


class ResearchError(W0Error):
    """Raised when a research fixture fails a domain gate."""


def require(mapping: dict[str, Any], key: str, context: str) -> Any:
    if key not in mapping:
        raise ResearchError(f"missing {key!r} in {context}")
    return mapping[key]


def positive_number(value: Any, context: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ResearchError(f"{context} must be numeric") from exc
    if number <= 0:
        raise ResearchError(f"{context} must be positive")
    return number


def timestamp(value: Any, context: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ResearchError(f"{context} must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchError(f"{context} is not an ISO timestamp: {value}") from exc


_positive_number = positive_number
_require = require
_timestamp = timestamp


def load_research_fixture(path: Path) -> dict[str, Any]:
    fixture = load_json(path)
    if require(fixture, "schema_version", "research fixture") != RESEARCH_FIXTURE_SCHEMA:
        raise ResearchError("unsupported research fixture schema")
    source = require(fixture, "source", "research fixture")
    if (
        source.get("rights_status") != "synthetic_test_data"
        or source.get("network_required") is not False
    ):
        raise ResearchError(
            "research fixture must declare synthetic_test_data and network_required=false"
        )
    for section in _REQUIRED_FIXTURE_SECTIONS:
        require(fixture, section, "research fixture")
    return fixture


def _validate_catalog_instrument(
    instrument: dict[str, Any], seen: dict[str, dict[str, Any]]
) -> None:
    stable_id = str(require(instrument, "stable_id", "catalog instrument"))
    if stable_id in seen:
        raise ResearchError(f"duplicate stable instrument identity: {stable_id}")
    for field in (
        "board",
        "security",
        "short_code",
        "expiry",
        "lot_size",
        "tick_size",
        "settlement_type",
    ):
        require(instrument, field, f"catalog instrument {stable_id}")
    if not isinstance(instrument["lot_size"], int) or instrument["lot_size"] <= 0:
        raise ResearchError(f"invalid lot size for {stable_id}")
    timestamp(instrument["expiry"], f"expiry for {stable_id}")
    if instrument.get("historical_status") == "current_snapshot":
        raise ResearchError(
            f"current snapshot cannot stand in for historical evidence: {stable_id}"
        )
    seen[stable_id] = instrument


def _collect_catalog_pages(
    pages: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[int]]:
    instruments: dict[str, dict[str, Any]] = {}
    page_numbers: list[int] = []
    for page in pages:
        page_no = int(require(page, "page_no", "catalog page"))
        page_numbers.append(page_no)
        require(page, "received_at", "catalog page")
        require(page, "raw_response_sha256", "catalog page")
        next_page = page.get("next_page")
        if next_page is not None and next_page != page_no + 1:
            raise ResearchError(f"catalog pagination jump after page {page_no}")
        items = require(page, "items", "catalog page")
        if not items:
            raise ResearchError("catalog page cannot be empty")
        for instrument in items:
            _validate_catalog_instrument(instrument, instruments)
    if sorted(page_numbers) != list(range(len(page_numbers))):
        raise ResearchError("catalog pages must be contiguous from zero")
    return instruments, page_numbers


def _validate_catalog_aliases(
    aliases: list[dict[str, Any]], instruments: dict[str, dict[str, Any]]
) -> None:
    for alias in aliases:
        stable_id = require(alias, "stable_id", "catalog alias")
        if stable_id not in instruments:
            raise ResearchError(f"alias points to unknown stable identity: {stable_id}")
        require(alias, "source_code", "catalog alias")


def _validate_price_rows(
    prices: list[dict[str, Any]], instruments: dict[str, dict[str, Any]]
) -> int:
    for row in prices:
        stable_id = require(row, "stable_id", "price row")
        if stable_id not in instruments:
            raise ResearchError(f"price row points to unknown instrument: {stable_id}")
        timestamp(row.get("event_time"), f"event_time for {stable_id}")
        timestamp(row.get("receipt_at"), f"receipt_at for {stable_id}")
        if row.get("source_vintage") in (None, ""):
            raise ResearchError(f"price row has no source vintage: {stable_id}")
        missing = _REQUIRED_PRICE_ROLES - set(row)
        if missing:
            raise ResearchError(f"price roles missing for {stable_id}: {sorted(missing)}")
        if row["bid"] > row["ask"]:
            raise ResearchError(f"bid exceeds ask for {stable_id}")
    return len(prices)


def _evaluate_catalog_candidates(
    candidates: list[dict[str, Any]], instruments: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for candidate in candidates:
        near = require(candidate, "near_stable_id", "candidate")
        far = require(candidate, "far_stable_id", "candidate")
        admitted = near in instruments and far in instruments and near != far
        results.append(
            {
                "candidate_id": require(candidate, "candidate_id", "candidate"),
                "near_stable_id": near,
                "far_stable_id": far,
                "status": "admitted" if admitted else "not_admitted",
                "reason": "stable identities and contract terms present"
                if admitted
                else "missing or repeated stable identity",
            }
        )
    if not any(result["status"] == "admitted" for result in results):
        raise ResearchError("catalog has no admitted candidate")
    return results


def run_w1_catalog(fixture: dict[str, Any]) -> dict[str, Any]:
    catalog = fixture["catalog"]
    instruments, page_numbers = _collect_catalog_pages(require(catalog, "pages", "catalog"))
    _validate_catalog_aliases(require(catalog, "aliases", "catalog"), instruments)
    price_rows = _validate_price_rows(fixture["prices"], instruments)
    candidate_results = _evaluate_catalog_candidates(
        require(catalog, "candidates", "catalog"), instruments
    )
    return {
        "wave": "W1",
        "status": "passed",
        "pages": sorted(page_numbers),
        "instrument_count": len(instruments),
        "price_row_count": price_rows,
        "price_roles": sorted(_REQUIRED_PRICE_ROLES),
        "candidates": candidate_results,
        "raw_response_provenance": True,
    }


def _classify_corporate_action(action: dict[str, Any]) -> tuple[str, bool, bool]:
    action_id = str(require(action, "action_id", "corporate action"))
    for field in ("source_role", "evidence_locator", "content_sha256", "event_type"):
        require(action, field, f"corporate action {action_id}")
    available_at = action.get("available_at")
    if available_at is not None:
        timestamp(available_at, f"available_at for {action_id}")
    is_conflict = action.get("conflict_status") == "conflict"
    signal_eligible = (
        available_at is not None
        and action.get("conflict_status") == "none"
        and action.get("source_role") == "primary"
    )
    return action_id, signal_eligible, is_conflict


def _validate_price_gap_checks(gap_checks: list[dict[str, Any]]) -> None:
    for check in gap_checks:
        if require(check, "diagnostic_type", "price gap check") != "price_gap":
            raise ResearchError("unknown price-gap diagnostic type")
        if require(check, "derived_only", "price gap check") is not True:
            raise ResearchError("price gap diagnostic must remain derived_only")
        if "action_id" in check:
            raise ResearchError("price gap diagnostic cannot overwrite a factual action")


def run_w2_corporate_actions(fixture: dict[str, Any]) -> dict[str, Any]:
    eligible: list[str] = []
    blocked: list[dict[str, str]] = []
    conflicts: list[str] = []
    for action in fixture["corporate_actions"]:
        action_id, is_eligible, is_conflict = _classify_corporate_action(action)
        if is_conflict:
            conflicts.append(action_id)
        if is_eligible:
            eligible.append(action_id)
        else:
            blocked.append(
                {
                    "action_id": action_id,
                    "reason": "missing availability vintage, non-primary source or unresolved conflict",
                }
            )
    gap_checks = fixture.get("price_gap_checks", [])
    _validate_price_gap_checks(gap_checks)
    if not eligible or not blocked or not conflicts:
        raise ResearchError(
            "corporate-action fixture must exercise eligible, blocked and conflict paths"
        )
    return {
        "wave": "W2",
        "status": "passed",
        "eligible_action_ids": sorted(eligible),
        "blocked_actions": blocked,
        "conflict_count": len(conflicts),
        "price_gap_diagnostics": len(gap_checks),
        "availability_vintage_enforced": True,
    }
