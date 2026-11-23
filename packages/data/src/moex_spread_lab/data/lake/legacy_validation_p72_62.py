"""Validate optional legacy close-price proxies."""

from pathlib import Path
from typing import Any


def validate_price_proxy(
    row: dict[str, Any], close: float | None, path: Path, index: int, error_type: type[Exception]
) -> None:
    price_proxy = row.get("price_proxy")
    if price_proxy is None:
        return
    if isinstance(price_proxy, bool) or not isinstance(price_proxy, (int, float, str)):
        raise error_type(f"legacy candle price_proxy is invalid: {path}[{index}]")
    try:
        proxy_value = float(price_proxy)
    except ValueError as exc:
        raise error_type(f"legacy candle price_proxy is not numeric: {path}[{index}]") from exc
    if proxy_value != close:
        raise error_type(f"legacy candle price_proxy differs from close: {path}[{index}]")
