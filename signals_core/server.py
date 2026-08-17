"""Widen the Companion personal-data bridge with a generic ``signals`` source.

The host ships a closed allowlist of personal-data sources
(``companion_api.PERSONAL_DATA_SOURCES``) and dispatches validation by source
id, so a snapshot of arbitrary labelled state has nowhere to land. This plugin
adds one: any paired publisher (an iOS Shortcut, a shell script, a Home
Assistant automation) PUTs rows to ``/api/app/v1/personal-data/signals`` and a
widget reads them back out of PERSONAL_DATA_STORE with no network access of its
own.

Everything the host already does for the bridge is inherited untouched: pairing
+ scoped bearer auth, latest-only-per-publisher storage, out-of-order and
conflict rejection, expiry tombstones, and the data-change refresh event
(``app.data_change_refresh`` already emits a whole-source event for source ids
it doesn't know, so no patch is needed there).

Two module attributes are patched, both read at request time rather than at
import, so no route needs re-registering:

  * ``PERSONAL_DATA_SOURCES`` — the guard in ``put_personal_data`` and the
    ``personal_data.sources`` list in the capability probe.
  * ``_validate_reminders_fridge`` — the ``else`` arm of the validator
    dispatch. Both host validators share a ``(source_id, body)`` signature, so
    ours wraps it and delegates anything that isn't ``signals``.

A host refactor that renames either attribute leaves the bridge exactly as it
shipped: the patch logs and does nothing rather than breaking uploads.
"""

from __future__ import annotations

import logging
from typing import Any

from app import companion_api as _host

SOURCE_ID = "signals"

MAX_ROWS = 64
MAX_ID = 128
MAX_LABEL = 128
MAX_VALUE = 256
MAX_UNIT = 16
MAX_STATE = 32

_SNAPSHOT_FIELDS = frozenset(("version", "source_id", "generated_at", "expires_at", "data"))
_ROW_FIELDS = frozenset(("id", "label", "value", "unit", "state", "at"))

logger = logging.getLogger(__name__)


def _validate_signals(
    source_id: str, body: Any
) -> tuple[tuple[dict[str, Any], float, float] | None, tuple[Any, int] | None]:
    """Validate a ``signals`` snapshot, mirroring the host's validator contract.

    Bounded on purpose: the store keeps whatever is accepted here in the clear
    until expiry, so the schema stays a fixed set of small scalar fields rather
    than "any JSON the publisher felt like sending".
    """

    def bad(msg: str) -> tuple[None, tuple[Any, int]]:
        return None, _host._error("invalid_snapshot", msg, 400)

    if not isinstance(body, dict):
        return bad("body must be a JSON object")
    if set(body) - _SNAPSHOT_FIELDS:
        return bad("snapshot has unexpected fields")
    if body.get("version") != _host.PERSONAL_DATA_SNAPSHOT_VERSION:
        return bad("unsupported snapshot version")
    if body.get("source_id") != source_id:
        return bad("source_id does not match the path")

    gen = _host._parse_iso(body.get("generated_at"))
    exp = _host._parse_iso(body.get("expires_at"))
    if gen is None or exp is None:
        return bad("generated_at and expires_at must be ISO 8601")
    if exp <= gen:
        return bad("expires_at must be after generated_at")
    if exp - gen > _host.PERSONAL_DATA_MAX_TTL_SECONDS:
        return bad("expires_at exceeds the maximum retention window")

    data = body.get("data")
    if not isinstance(data, dict) or set(data) - {"rows"}:
        return bad("data must be an object with only rows")
    rows = data.get("rows")
    if not isinstance(rows, list):
        return bad("data.rows must be an array")
    if not rows:
        return bad("data.rows must not be empty")
    if len(rows) > MAX_ROWS:
        return bad(f"too many rows (max {MAX_ROWS})")

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return bad("each row must be an object")
        if set(row) - _ROW_FIELDS:
            return bad("a row has unexpected fields")

        row_id = row.get("id")
        if not isinstance(row_id, str) or not (1 <= len(row_id) <= MAX_ID):
            return bad(f"row id must be a 1-{MAX_ID} char string")
        if row_id in seen:
            return bad("row ids must be unique")
        seen.add(row_id)

        label = row.get("label")
        if not isinstance(label, str) or not (1 <= len(label) <= MAX_LABEL):
            return bad(f"row label must be a 1-{MAX_LABEL} char string")

        if "value" not in row:
            return bad("row value is required")
        value = row.get("value")
        if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
            pass
        elif isinstance(value, str):
            if len(value) > MAX_VALUE:
                return bad(f"row value must be at most {MAX_VALUE} chars")
        else:
            return bad("row value must be a string, number, boolean or null")

        unit = row.get("unit")
        if unit is not None and (not isinstance(unit, str) or len(unit) > MAX_UNIT):
            return bad(f"row unit must be a string of at most {MAX_UNIT} chars or null")

        state = row.get("state")
        if state is not None and (not isinstance(state, str) or len(state) > MAX_STATE):
            return bad(f"row state must be a string of at most {MAX_STATE} chars or null")

        at = row.get("at")
        if at is not None and _host._parse_iso(at) is None:
            return bad("row at must be ISO 8601 or null")

    return (body, gen, exp), None


def _install() -> None:
    sources = getattr(_host, "PERSONAL_DATA_SOURCES", None)
    fallback = getattr(_host, "_validate_reminders_fridge", None)
    if not isinstance(sources, tuple) or not callable(fallback):
        logger.warning(
            "signals_core: host personal-data bridge looks different than expected "
            "(PERSONAL_DATA_SOURCES/_validate_reminders_fridge); leaving it alone"
        )
        return
    if SOURCE_ID in sources:
        return

    def dispatch(
        source_id: str, body: Any
    ) -> tuple[tuple[dict[str, Any], float, float] | None, tuple[Any, int] | None]:
        if source_id == SOURCE_ID:
            return _validate_signals(source_id, body)
        return fallback(source_id, body)  # type: ignore[no-any-return]

    _host._validate_reminders_fridge = dispatch
    _host.PERSONAL_DATA_SOURCES = (*sources, SOURCE_ID)
    logger.info("signals_core: personal-data source %r registered", SOURCE_ID)


_install()
