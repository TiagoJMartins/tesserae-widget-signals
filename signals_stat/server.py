"""signals_stat: read one published signal out of the Companion personal-data store.

No network access of any kind: ``signals_core`` registers the source family, a
paired publisher PUTs snapshots, and this reads whatever the store holds. Each
signal is its own source (``signals.<id>``), so a cell selects a signal and
reads that one record, newest publisher wins on a genuine collision. Freshness
is reported rather than hidden, because a publisher that stopped is the one
failure a beacon must not render as OFF.
"""

from __future__ import annotations

import time
from typing import Any

from flask import current_app

SOURCE_ID = "signals"
SIGNAL_PREFIX = f"{SOURCE_ID}."

# The host's own bound, mirrored so the widget can say which one bit: the bridge
# marks a snapshot stale at 24 h (and drops its values at 48 h). Both are
# server-side facts, not options.
HOST_STALE_SECONDS = 86_400


def _store() -> Any:
    return current_app.config.get("PERSONAL_DATA_STORE")


def _signal_ids() -> list[str]:
    """Every published ``signals.<id>``, as bare ids. Includes expired
    tombstones, so the picker still lists a signal whose publisher went quiet."""
    store = _store()
    if store is None:
        return []
    lister = getattr(store, "all", None)
    if not callable(lister):
        return []
    known = lister()
    if not isinstance(known, dict):
        return []
    return sorted(
        sid[len(SIGNAL_PREFIX) :]
        for sid in known
        if isinstance(sid, str) and sid.startswith(SIGNAL_PREFIX)
    )


def _records(signal_id: str) -> list[dict[str, Any]]:
    """Every publisher's record for one signal id."""
    store = _store()
    if store is None:
        return []
    publications = getattr(store, "publications", None)
    if callable(publications):
        records = publications(SIGNAL_PREFIX + signal_id)
        if isinstance(records, list):
            return [r for r in records if isinstance(r, dict)]
    return []


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


def _first_label(signal_id: str) -> str:
    """A friendlier picker label than the raw id: the newest live record's first
    row label, falling back to the id itself when nothing readable is left."""
    live = [r for r in _records(signal_id) if _rows(r)]
    if not live:
        return signal_id
    record = max(live, key=lambda r: float(r.get("generated_epoch") or 0))
    label = str((_rows(record)[0]).get("label") or "").strip()
    return label or signal_id


def choices(name: str) -> list[dict[str, str]]:
    """Populate the editor's dropdowns from what has actually been published.

    ``signals`` lists every published signal id; ``rows`` lists the rows inside
    them, so a signal that groups several values can have one picked. Both come
    from the store, so neither is a free-text field.
    """
    if name == "signals":
        out = [
            {"value": signal_id, "label": _first_label(signal_id)}
            for signal_id in _signal_ids()
        ]
        return sorted(out, key=lambda item: (item["label"].casefold(), item["value"]))
    if name == "rows":
        signal_ids = _signal_ids()
        ambiguous = len(signal_ids) > 1
        out = []
        for signal_id in signal_ids:
            seen: set[str] = set()
            for record in _records(signal_id):
                for row in _rows(record):
                    row_id = str(row.get("id") or "").strip()
                    if not row_id or row_id in seen:
                        continue
                    seen.add(row_id)
                    label = str(row.get("label") or "").strip() or row_id
                    out.append(
                        {
                            "value": row_id,
                            "label": f"{signal_id} · {label}" if ambiguous else label,
                        }
                    )
        return sorted(out, key=lambda item: (item["label"].casefold(), item["value"]))
    return []


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


def _pick_row(rows: list[dict[str, Any]], row_id: str) -> dict[str, Any] | None:
    """The chosen row, or the first when none is chosen. A named row that isn't
    present returns None rather than silently falling back to another."""
    if not rows:
        return None
    if row_id:
        for row in rows:
            if str(row.get("id") or "") == row_id:
                return row
        return None
    return rows[0]


def fetch(options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]) -> dict[str, Any]:
    del settings, ctx
    signal_id = str(options.get("signal") or "").strip()
    row_id = str(options.get("row_id") or "").strip()
    stale_after = max(0, _num(options.get("stale_after"), 900))
    now = time.time()

    if not _signal_ids():
        return {
            "state": "unpublished",
            "message": "Nothing published to the signals bridge yet.",
        }
    if not signal_id:
        return {"state": "unconfigured", "message": "Pick a signal in this cell's settings."}

    records = _records(signal_id)
    # An expired snapshot is redacted to a timestamp-only tombstone, so a signal
    # that used to exist looks identical to one that never did. Say which:
    # "expired" is a publisher that stopped, "missing" is a signal that isn't in
    # a live snapshot. Either way never fall back to another signal or row, or a
    # dashboard quietly shows someone else's state under the old title.
    live = [record for record in records if not record.get("expired") and _rows(record)]
    if not live:
        if records:
            return {
                "state": "expired",
                "message": "The last snapshot expired.",
                "signal": signal_id,
                "row_id": row_id,
            }
        return {
            "state": "missing",
            "message": "That signal isn't published.",
            "signal": signal_id,
            "row_id": row_id,
        }

    record = max(live, key=lambda r: float(r.get("generated_epoch") or 0))
    row = _pick_row(_rows(record), row_id)
    if row is None:
        return {
            "state": "missing",
            "message": "That signal isn't in the latest snapshot.",
            "signal": signal_id,
            "row_id": row_id,
        }

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
        "signal": signal_id,
        "row_id": str(row.get("id") or ""),
        "label": str(row.get("label") or row.get("id") or signal_id),
        "value": value,
        # Kept separate from ``value`` so the client can match ON states without
        # having to guess whether a publisher used state names or bare values.
        "row_state": row.get("state"),
        "unit": row.get("unit"),
        "age": age,
        "publisher": _publisher(record),
    }
