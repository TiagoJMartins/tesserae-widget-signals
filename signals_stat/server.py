"""signals_stat: read one pushed row out of the Companion personal-data store.

No network access of any kind: ``signals_core`` registers the source, a paired
publisher PUTs snapshots, and this reads whatever the store holds. Freshness is
reported rather than hidden, because a publisher that stopped is the one failure
a beacon must not render as OFF.
"""

from __future__ import annotations

import time
from typing import Any

from flask import current_app

SOURCE_ID = "signals"

# The host's own bounds, mirrored so the widget can say which one bit. Both are
# server-side facts, not options: the bridge marks a snapshot stale at 24 h and
# drops its values at 48 h.
HOST_STALE_SECONDS = 86_400


def _records() -> list[dict[str, Any]]:
    store = current_app.config.get("PERSONAL_DATA_STORE")
    if store is None:
        return []
    publications = getattr(store, "publications", None)
    if callable(publications):
        records = publications(SOURCE_ID)
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    record = store.get(SOURCE_ID)
    return [record] if isinstance(record, dict) else []


def _rows(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    if record is None:
        return []
    snapshot = record.get("snapshot")
    if not isinstance(snapshot, dict):
        return []
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("rows")
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _publisher(record: dict[str, Any]) -> str:
    return str(record.get("publisher_name") or "Publisher").strip() or "Publisher"


def choices(name: str) -> list[dict[str, str]]:
    """Every published row, so the editor's picker isn't a free-text id field."""
    if name != "rows":
        return []
    records = _records()
    multi = len({str(r.get("publisher_id") or "") for r in records if _rows(r)}) > 1
    out: list[dict[str, str]] = []
    for record in records:
        publisher = _publisher(record)
        for row in _rows(record):
            row_id = str(row.get("id") or "").strip()
            label = str(row.get("label") or "").strip() or row_id
            if not row_id:
                continue
            out.append(
                {"value": row_id, "label": f"{publisher} · {label}" if multi else label}
            )
    return sorted(out, key=lambda item: (item["label"].casefold(), item["value"]))


def _parse_iso(raw: Any) -> float | None:
    """Epoch seconds for an ISO-8601 string, or None.

    Only used for a row's optional ``at``; the envelope's epochs are already
    numbers on the stored record.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        from datetime import UTC

        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


def _num(raw: Any, fallback: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def fetch(options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
    del settings, ctx
    row_id = str(options.get("row_id") or "").strip()
    stale_after = max(0, _num(options.get("stale_after"), 900))
    now = time.time()

    records = _records()
    if not records:
        return {
            "state": "unpublished",
            "message": "Nothing published to the signals bridge yet.",
        }
    if not row_id:
        return {"state": "unconfigured", "message": "Pick a signal in this cell's settings."}

    found: tuple[dict[str, Any], dict[str, Any]] | None = None
    for record in records:
        for row in _rows(record):
            if str(row.get("id") or "") == row_id:
                generated = record.get("generated_epoch")
                if found is None or float(generated or 0) > float(
                    found[0].get("generated_epoch") or 0
                ):
                    found = (record, row)
    if found is None:
        # An expired snapshot is redacted to a timestamp-only tombstone, so a
        # row that used to exist looks identical to one that never did. Say
        # which: "expired" is a publisher that stopped, "missing" is a signal
        # that isn't in a live snapshot. Either way never fall back to another
        # row, or a dashboard quietly shows someone else's state under the old
        # title.
        if all(record.get("expired") for record in records):
            return {
                "state": "expired",
                "message": "The last snapshot expired.",
                "row_id": row_id,
            }
        return {
            "state": "missing",
            "message": "That signal isn't in the latest snapshot.",
            "row_id": row_id,
        }

    record, row = found
    generated = float(record.get("generated_epoch") or 0)
    expires = float(record.get("expires_epoch") or 0)
    sampled = _parse_iso(row.get("at")) or generated
    age = max(0, int(now - sampled))

    if expires and now >= expires:
        state = "expired"
    elif stale_after and age >= stale_after:
        state = "stale"
    elif now - generated >= HOST_STALE_SECONDS:
        state = "stale"
    else:
        state = "fresh"

    value = row.get("value")
    return {
        "state": state,
        "row_id": row_id,
        "label": str(row.get("label") or row_id),
        "value": value,
        # Kept separate from ``value`` so the client can match ON states without
        # having to guess whether a publisher used state names or bare values.
        "row_state": row.get("state"),
        "unit": row.get("unit"),
        "age": age,
        "publisher": _publisher(record),
    }
