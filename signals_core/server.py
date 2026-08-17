"""Widen the Companion personal-data bridge with a per-signal ``signals`` family.

The host ships a closed allowlist of personal-data sources
(``companion_api.PERSONAL_DATA_SOURCES``), dispatches validation by source id,
and keeps exactly one snapshot per ``(publisher, source_id)`` — replaced whole
on each accepted PUT. A single flat ``signals`` source therefore made two
devices sharing one paired credential clobber each other: the second device's
PUT replaced the first's snapshot even though every row had a distinct owner.

This plugin keys state by signal id instead, mirroring the host's own
``reminders`` / ``reminders.fridge`` convention. A publisher PUTs to
``/api/app/v1/personal-data/signals.<signal_id>`` (or the bare ``signals``
family root, still accepted), so each signal is its own store row with its own
owner and its own expiry, and one shared credential is safe across devices.

Everything the host already does for the bridge is inherited untouched: pairing
+ scoped bearer auth, out-of-order and conflict rejection, expiry tombstones.
Four module attributes are patched, all read at request time rather than at
import, so no route needs re-registering:

  * ``PERSONAL_DATA_SOURCES`` — the ``source_id not in`` guard in
    ``put_personal_data`` and the ``personal_data.sources`` list in the
    capability probe. Replaced with a container that admits the family root and
    any ``signals.<slug>`` on ``__contains__`` and enumerates the concrete ids
    actually in the store on ``__iter__``, so the probe advertises real sources
    rather than an open-ended pattern.
  * ``_validate_reminders_fridge`` — the ``else`` arm of the validator
    dispatch. Anything in the ``signals`` family routes to our validator; every
    other source id delegates to the host's original.
  * ``personal_data_update_event`` / ``personal_data_delete_event`` — the host
    builds a change event whose source is ``personal_data.<source_id>``, so a
    ``signals.<id>`` PUT would emit ``personal_data.signals.<id>`` and match no
    static ``on_change`` declaration. For a family-member source the event is
    rewritten to source ``personal_data.signals`` carrying the signal id as its
    lone selector, which is what ``signals_stat`` subscribes to. Every other
    source's event passes through unchanged.
  * ``_valid_client`` — pairing hard-rejects any platform but ``ios``. A Mac or
    a Pi publishing its own state shouldn't have to claim to be an iPhone, so a
    few more platform names are accepted and the rest of the host's client
    validation is reused as-is.

A host refactor that renames any of them leaves that part of the bridge exactly
as it shipped: the patch logs and does nothing rather than breaking uploads.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from flask import current_app, has_app_context

from app import companion_api as _host

SOURCE_ID = "signals"

# A signal id is the second segment of ``signals.<id>``. Strict and lowercase
# on purpose: it becomes a store key and part of a source id, so a loose
# character set would let a typo mint a permanent orphan source.
_SIGNAL_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# Platforms accepted at pairing on top of the host's ``ios``. Kept short and
# explicit; this is a label on a credential, not a capability gate.
EXTRA_PLATFORMS = frozenset(("macos", "linux", "shortcuts", "homeassistant"))

MAX_ROWS = 64
MAX_ID = 128
MAX_LABEL = 128
MAX_VALUE = 256
MAX_UNIT = 16
MAX_STATE = 32

_SNAPSHOT_FIELDS = frozenset(("version", "source_id", "generated_at", "expires_at", "data"))
_ROW_FIELDS = frozenset(("id", "label", "value", "unit", "state", "at"))

logger = logging.getLogger(__name__)


def _signal_id(source_id: Any) -> str | None:
    """The ``<id>`` in ``signals.<id>`` when valid, else None.

    The bare family root returns None (it carries no id); a member with a
    malformed suffix also returns None, so a typo is refused at the guard rather
    than stored as a source no widget will ever pick.
    """
    if not isinstance(source_id, str):
        return None
    prefix = f"{SOURCE_ID}."
    if not source_id.startswith(prefix):
        return None
    candidate = source_id[len(prefix) :]
    return candidate if _SIGNAL_ID_RE.match(candidate) else None


def _in_family(source_id: Any) -> bool:
    return source_id == SOURCE_ID or _signal_id(source_id) is not None


def _known_signal_sources() -> list[str]:
    """Concrete ``signals.*`` source ids currently in the store.

    Read at request time from the app's store; empty outside an app context
    (import, say) so the capability probe never advertises a fabricated id.
    """
    if not has_app_context():
        return []
    store = current_app.config.get("PERSONAL_DATA_STORE")
    if store is None:
        return []
    lister = getattr(store, "all", None)
    if not callable(lister):
        return []
    try:
        known = lister()
    except Exception:
        return []
    if not isinstance(known, dict):
        return []
    return sorted(sid for sid in known if _signal_id(sid) is not None)


class _SignalsSources:
    """Source allowlist admitting the whole ``signals`` family.

    Wraps the host's original tuple so ``reminders`` and ``reminders.fridge``
    still resolve. ``__contains__`` backs the ``put_personal_data`` guard;
    ``__iter__`` backs ``list(PERSONAL_DATA_SOURCES)`` in the capability probe,
    reporting the family root plus every signal id the store actually holds.
    """

    def __init__(self, base: tuple[str, ...]) -> None:
        self._base = tuple(base)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._base or _in_family(source_id)

    def __iter__(self) -> Any:
        ordered = list(self._base)
        for sid in (SOURCE_ID, *_known_signal_sources()):
            if sid not in ordered:
                ordered.append(sid)
        return iter(ordered)

    def __repr__(self) -> str:
        return f"_SignalsSources({self._base!r})"


def _validate_signals(
    source_id: str, body: Any
) -> tuple[tuple[dict[str, Any], float, float] | None, tuple[Any, int] | None]:
    """Validate a ``signals`` snapshot, mirroring the host's validator contract.

    Bounded on purpose: the store keeps whatever is accepted here in the clear
    until expiry, so the schema stays a fixed set of small scalar fields rather
    than "any JSON the publisher felt like sending". The same validator serves
    the family root and every ``signals.<id>`` member; ``source_id`` is checked
    against the request path exactly as the host does for its own sources.
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


def _refocus_event(event: Any, signal_id: str) -> Any:
    """Move the signal id from the event's source into its selector set.

    The host keys the change event by the full source id, but a static
    ``on_change`` subscription can only name ``personal_data.signals``; the
    signal id has to travel as a selector so ``_placement_matches`` narrows the
    refresh to the cells that picked it. Preserves a ``None`` (an accepted PUT
    that changed nothing emits no event).
    """
    if event is None:
        return None
    return _host.DataChangeEvent(
        source=f"personal_data.{SOURCE_ID}",
        selectors=frozenset({signal_id}),
    )


def _install_events() -> None:
    original_update = getattr(_host, "personal_data_update_event", None)
    original_delete = getattr(_host, "personal_data_delete_event", None)
    if (
        not callable(original_update)
        or not callable(original_delete)
        or not callable(getattr(_host, "DataChangeEvent", None))
    ):
        logger.warning(
            "signals_core: personal-data change-event builders look different than "
            "expected; per-signal refresh selectors left uninstalled"
        )
        return

    def update(source_id: str, previous: Any, current: Any) -> Any:
        event = original_update(source_id, previous, current)
        signal_id = _signal_id(source_id)
        return _refocus_event(event, signal_id) if signal_id is not None else event

    def delete(source_id: str, previous: Any = None) -> Any:
        event = original_delete(source_id, previous)
        signal_id = _signal_id(source_id)
        return _refocus_event(event, signal_id) if signal_id is not None else event

    _host.personal_data_update_event = update
    _host.personal_data_delete_event = delete
    logger.info("signals_core: per-signal change-event selectors installed")


def _install_platforms() -> None:
    """Let a non-iOS publisher pair as itself.

    ``_valid_client`` hard-rejects any ``platform`` but ``"ios"``, which is
    right for the Companion app and wrong for the publishers this source exists
    to serve: a Mac, a Pi, a Shortcut. Rather than have them claim to be an
    iPhone, accept a short allowlist and reuse the host's validation for
    everything else (name length, app_version, installation_id bounds) by
    handing it an ios-shaped copy and restoring the real platform after.
    """
    original = getattr(_host, "_valid_client", None)
    if not callable(original):
        logger.warning("signals_core: no _valid_client to widen; iOS-only pairing stands")
        return

    def widened(client: Any) -> dict[str, Any] | None:
        if not isinstance(client, dict):
            return original(client)  # type: ignore[misc]
        platform = client.get("platform")
        if platform == "ios" or platform not in EXTRA_PLATFORMS:
            return original(client)  # type: ignore[misc]
        checked = original({**client, "platform": "ios"})  # type: ignore[misc]
        if checked is None:
            return None
        return {**checked, "platform": platform}

    _host._valid_client = widened
    logger.info("signals_core: pairing widened to platforms %s", sorted(EXTRA_PLATFORMS))


def _install() -> None:
    sources = getattr(_host, "PERSONAL_DATA_SOURCES", None)
    fallback = getattr(_host, "_validate_reminders_fridge", None)
    if not isinstance(sources, tuple) or not callable(fallback):
        logger.warning(
            "signals_core: host personal-data bridge looks different than expected "
            "(PERSONAL_DATA_SOURCES/_validate_reminders_fridge); leaving it alone"
        )
        return

    def dispatch(
        source_id: str, body: Any
    ) -> tuple[tuple[dict[str, Any], float, float] | None, tuple[Any, int] | None]:
        if _in_family(source_id):
            return _validate_signals(source_id, body)
        return fallback(source_id, body)  # type: ignore[no-any-return]

    _host._validate_reminders_fridge = dispatch
    _host.PERSONAL_DATA_SOURCES = _SignalsSources(sources)
    logger.info("signals_core: personal-data source family %r registered", SOURCE_ID)


_install()
_install_platforms()
_install_events()
