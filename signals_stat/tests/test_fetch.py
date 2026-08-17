"""signals_stat's read path, against a real app with real published snapshots.

The store is the contract between the bridge and the widget, so these publish
through the actual PUT route rather than hand-building store records.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
BUNDLE = ("signals_core", "signals_stat")

_root = os.environ.get("TESSERAE_ROOT", str(Path.home() / "Projects/TiagoJMartins/tesserae"))
TESSERAE_ROOT = Path(_root).expanduser()
if TESSERAE_ROOT.is_dir() and str(TESSERAE_ROOT) not in sys.path:
    sys.path.insert(0, str(TESSERAE_ROOT))

app_factory = pytest.importorskip(
    "app.app_factory", reason=f"no Tesserae checkout at {TESSERAE_ROOT}"
)

CLIENT = {
    "name": "Kitchen Mac",
    "platform": "macos",
    "app_version": "0.1.0",
    "installation_id": "A1B2C3D4-E5F6-47A8-9012-3456789ABCDE",
}
CLIENT_B = {**CLIENT, "name": "Studio Mac", "installation_id": "B1B2C3D4-E5F6-47A8-9012-3456789ABCDE"}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(generated: datetime, rows: list[dict[str, Any]], *, ttl_hours: int = 12) -> dict:
    return {
        "version": "personal_data_bridge_v1",
        "source_id": "signals",
        "generated_at": _iso(generated),
        "expires_at": _iso(generated + timedelta(hours=ttl_hours)),
        "data": {"rows": rows},
    }


def _row(row_id: str, **over: Any) -> dict[str, Any]:
    return {
        "id": row_id,
        "label": over.pop("label", row_id.replace("_", " ").title()),
        "value": over.pop("value", "on"),
        "unit": over.pop("unit", None),
        "state": over.pop("state", "alert"),
        "at": over.pop("at", None),
        **over,
    }


@pytest.fixture
def app(tmp_path: Path):
    authored = tmp_path / "authored"
    authored.mkdir(parents=True, exist_ok=True)
    for folder in BUNDLE:
        shutil.copytree(
            REPO / folder, authored / folder, ignore=shutil.ignore_patterns("tests")
        )

    a = app_factory.create_app(
        testing=False,
        data_root=tmp_path,
        plugins_dir=TESSERAE_ROOT / "plugins",
        renderers_dir=TESSERAE_ROOT / "renderers",
        devices_dir=TESSERAE_ROOT / "devices",
    )
    a.config["TESTING"] = True
    assert [e.message for e in a.config["PLUGIN_REGISTRY"].errors] == []
    return a


@pytest.fixture
def server(app) -> ModuleType:
    """The widget's own server module, as the host loaded it."""
    plugin = app.config["PLUGIN_REGISTRY"].plugins["signals_stat"]
    assert plugin.server_module is not None
    return plugin.server_module


def _pair(app, client: dict[str, str]) -> dict[str, str]:
    code = app.config["COMPANION_PAIRING_STORE"].issue(note="test").code
    paired = (
        app.test_client()
        .post(
            "/api/app/v1/pair",
            data=json.dumps({"code": code, "client": client}),
            content_type="application/json",
        )
        .get_json()
    )
    return {"Authorization": f"Bearer {paired['token']}", "Content-Type": "application/json"}


def _publish(app, rows: list[dict[str, Any]], *, client=CLIENT, generated=None, ttl_hours=12):
    auth = _pair(app, client)
    body = _snapshot(generated or datetime.now(UTC).replace(microsecond=0), rows, ttl_hours=ttl_hours)
    resp = app.test_client().put(
        "/api/app/v1/personal-data/signals", data=json.dumps(body), headers=auth
    )
    assert resp.status_code == 200, resp.get_json()


def _fetch(app, server: ModuleType, **options: Any) -> dict[str, Any]:
    with app.app_context():
        return server.fetch({"stale_after": 900, **options}, {}, ctx={})


def test_nothing_published(app, server) -> None:
    assert _fetch(app, server, row_id="slack_unread")["state"] == "unpublished"


def test_published_but_unconfigured(app, server) -> None:
    _publish(app, [_row("slack_unread")])
    assert _fetch(app, server)["state"] == "unconfigured"


def test_reads_the_selected_row(app, server) -> None:
    _publish(app, [_row("slack_unread", value="on"), _row("build", value=3, unit="fails")])

    out = _fetch(app, server, row_id="build")
    assert out["state"] == "fresh"
    assert out["label"] == "Build"
    assert out["value"] == 3
    assert out["unit"] == "fails"
    assert out["publisher"] == "Kitchen Mac"
    assert out["age"] < 5


def test_missing_row_does_not_fall_back(app, server) -> None:
    _publish(app, [_row("slack_unread")])
    out = _fetch(app, server, row_id="gone")
    assert out["state"] == "missing"
    assert out["row_id"] == "gone"


def test_row_timestamp_drives_age(app, server) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _publish(app, [_row("slack_unread", at=_iso(now - timedelta(minutes=30)))], generated=now)

    fresh_enough = _fetch(app, server, row_id="slack_unread", stale_after=0)
    assert fresh_enough["state"] == "fresh"
    assert 1700 < fresh_enough["age"] < 1900

    assert _fetch(app, server, row_id="slack_unread", stale_after=900)["state"] == "stale"


def test_expired_snapshot_reads_as_expired(app, server) -> None:
    # The store keeps a tombstone past expiry; the widget must call that out
    # rather than render the last value it saw.
    now = datetime.now(UTC).replace(microsecond=0)
    _publish(app, [_row("slack_unread")], generated=now)
    with app.app_context():
        store = app.config["PERSONAL_DATA_STORE"]
        record = store.publications("signals")[0]
        store.put(
            "signals",
            snapshot=record["snapshot"],
            generated_epoch=record["generated_epoch"],
            expires_epoch=record["generated_epoch"] - 1,
            publisher_id=record["publisher_id"],
            publisher_name=record["publisher_name"],
        )
    assert _fetch(app, server, row_id="slack_unread")["state"] == "expired"


def test_newest_publisher_wins_for_a_shared_row_id(app, server) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    _publish(app, [_row("slack_unread", label="Older")], client=CLIENT, generated=now - timedelta(hours=2))
    _publish(app, [_row("slack_unread", label="Newer")], client=CLIENT_B, generated=now)

    assert _fetch(app, server, row_id="slack_unread")["publisher"] == "Studio Mac"


def test_choices_lists_rows_and_names_publishers_when_ambiguous(app, server) -> None:
    _publish(app, [_row("slack_unread", label="Slack"), _row("build", label="Build")])
    with app.app_context():
        single = server.choices("rows")
    assert [c["label"] for c in single] == ["Build", "Slack"]
    assert single[0]["value"] == "build"

    _publish(app, [_row("vpn", label="VPN")], client=CLIENT_B)
    with app.app_context():
        multi = server.choices("rows")
    assert "Studio Mac · VPN" in [c["label"] for c in multi]
    assert server.choices("nope") == []
