"""Parse Kalshi's CF Benchmarks passthrough (BRTI / ERTI)."""

from __future__ import annotations

from typing import Any

INDEX_BY_ASSET = {"BTC": "BRTI", "ETH": "ERTI"}


def parse_cf_index_value(blob: object) -> float | None:
    """Pull a positive index level out of CF / Kalshi envelope shapes."""
    if blob is None or blob == "" or blob == {} or blob == []:
        return None
    if isinstance(blob, bool):
        return None
    if isinstance(blob, (int, float)):
        return float(blob) if blob > 0 else None
    if isinstance(blob, str):
        try:
            number = float(blob.strip())
        except ValueError:
            return None
        return number if number > 0 else None
    if isinstance(blob, list):
        for item in blob:
            parsed = parse_cf_index_value(item)
            if parsed:
                return parsed
        return None
    if not isinstance(blob, dict):
        return None
    for key in (
        "value",
        "VALUE",
        "indexValue",
        "index_value",
        "price",
        "last",
        "payload",
        "data",
        "values",
        "elements",
    ):
        if key not in blob:
            continue
        parsed = parse_cf_index_value(blob[key])
        if parsed:
            return parsed
    return None


def index_id_for(asset: str) -> str | None:
    return INDEX_BY_ASSET.get(str(asset or "").upper())
