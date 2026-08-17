"""The bridge patch, exercised against a real Tesserae app.

Runs the host's own Flask factory with this plugin dropped into
``<data_root>/authored/``, the way the authored-plugin path lands one. Needs a
Tesserae checkout on ``sys.path``:

    TESSERAE_ROOT=~/Projects/TiagoJMartins/tesserae \\
      ~/Projects/TiagoJMartins/tesserae/.venv/bin/python -m pytest signals_core/ -q

Skipped when that isn't available, so the file is safe to collect anywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_ID = PLUGIN_DIR.name

_root = os.environ.get("TESSERAE_ROOT", str(Path.home() / "Projects/TiagoJMartins/tesserae"))
TESSERAE_ROOT = Path(_root).expanduser()
if TESSERAE_ROOT.is_dir() and str(TESSERAE_ROOT) not in sys.path:
    sys.path.insert(0, str(TESSERAE_ROOT))

app_factory = pytest.importorskip(
    "app.app_factory", reason=f"no Tesserae checkout at {TESSERAE_ROOT}"
)

CLIENT = {
    "name": "Test Publisher",
    "platform": "ios",
    "app_version": "0.1.0",
    "installation_id": "A1B2C3D4-E5F6-47A8-9012-3456789ABCDE",
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot(generated: datetime, *, value: Any = "on", ttl_hours: int = 12) -> dict[str, Any]:
    return {
        "version": "personal_data_bridge_v1",
        "source_id": "signals",
        "generated_at": _iso(generated),
        "expires_at": _iso(generated + timedelta(hours=ttl_hours)),
        "data": {
            "rows": [
                {
                    "id": "slack_unread",
                    "label": "Slack",
                    "value": value,
                    "unit": None,
                    "state": "alert",
                    "at": _iso(generated),
                }
            ]
        },
    }


@pytest.fixture
def app(tmp_path: Path):
    authored = tmp_path / "authored"
    authored.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGIN_DIR, authored / PLUGIN_ID, ignore=shutil.ignore_patterns("tests"))

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
def auth(app) -> dict[str, str]:
    code = app.config["COMPANION_PAIRING_STORE"].issue(note="test").code
    paired = (
        app.test_client()
        .post(
            "/api/app/v1/pair",
            data=json.dumps({"code": code, "client": CLIENT}),
            content_type="application/json",
        )
        .get_json()
    )
    assert "personal_data:write" in paired["scopes"]
    return {"Authorization": f"Bearer {paired['token']}", "Content-Type": "application/json"}


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _put(app, auth, body: dict[str, Any], source: str = "signals"):
    return app.test_client().put(
        f"/api/app/v1/personal-data/{source}", data=json.dumps(body), headers=auth
    )


def test_source_is_advertised(app, auth) -> None:
    probe = app.test_client().get("/api/app/v1/", headers=auth).get_json()
    assert "signals" in probe["personal_data"]["sources"]


def test_host_sources_survive_the_patch(app, auth) -> None:
    from app import companion_api

    assert companion_api.PERSONAL_DATA_SOURCES[:2] == ("reminders", "reminders.fridge")


def _pair(app, client: dict[str, str]):
    code = app.config["COMPANION_PAIRING_STORE"].issue(note="test").code
    return app.test_client().post(
        "/api/app/v1/pair",
        data=json.dumps({"code": code, "client": client}),
        content_type="application/json",
    )


@pytest.mark.parametrize("platform", ["macos", "linux", "shortcuts", "homeassistant"])
def test_non_ios_publishers_can_pair_as_themselves(app, platform) -> None:
    resp = _pair(app, {**CLIENT, "platform": platform})
    assert resp.status_code == 201, resp.get_json()
    assert "personal_data:write" in resp.get_json()["scopes"]


def test_pairing_still_rejects_junk_clients(app) -> None:
    # The widened path must reuse the host's other client checks, not skip them.
    assert _pair(app, {**CLIENT, "platform": "toaster"}).status_code == 400
    assert _pair(app, {**CLIENT, "platform": "macos", "installation_id": "short"}).status_code == 400
    assert _pair(app, {**CLIENT, "platform": "macos", "name": ""}).status_code == 400


def test_accepted_snapshot_reaches_the_widget_side(app, auth, now) -> None:
    assert _put(app, auth, _snapshot(now)).status_code == 200

    with app.app_context():
        records = app.config["PERSONAL_DATA_STORE"].publications("signals")
    assert len(records) == 1
    row = records[0]["snapshot"]["data"]["rows"][0]
    assert row["id"] == "slack_unread"
    assert row["value"] == "on"
    assert records[0]["publisher_id"].startswith("companion_")


def test_value_accepts_scalars(app, auth, now) -> None:
    for offset, value in enumerate((42, 3.5, True, None, "warm")):
        stamp = now + timedelta(minutes=offset)
        assert _put(app, auth, _snapshot(stamp, value=value)).status_code == 200


def test_replay_is_idempotent_and_older_is_refused(app, auth, now) -> None:
    body = _snapshot(now)
    assert _put(app, auth, body).status_code == 200
    assert _put(app, auth, body).status_code == 200

    stale = _put(app, auth, _snapshot(now - timedelta(minutes=5)))
    assert stale.status_code == 409
    assert stale.get_json()["error"]["code"] == "snapshot_out_of_order"


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda b: b["data"]["rows"][0].update(nope=1), "unexpected fields"),
        (lambda b: b["data"].update(extra=[]), "only rows"),
        (lambda b: b["data"].update(rows=[]), "must not be empty"),
        (lambda b: b["data"]["rows"][0].update(id=""), "1-128 char string"),
        (lambda b: b["data"]["rows"][0].update(value={"a": 1}), "string, number, boolean or null"),
        (lambda b: b["data"]["rows"][0].update(at="not-a-timestamp"), "ISO 8601"),
        (lambda b: b.update(version="nope"), "unsupported snapshot version"),
        (lambda b: b.update(expires_at=b["generated_at"]), "must be after"),
    ],
)
def test_malformed_snapshots_are_refused(app, auth, now, mutate, fragment) -> None:
    body = _snapshot(now)
    mutate(body)
    resp = _put(app, auth, body)
    assert resp.status_code == 400
    assert fragment in resp.get_json()["error"]["message"]


def test_ttl_cap_is_enforced(app, auth, now) -> None:
    resp = _put(app, auth, _snapshot(now, ttl_hours=72))
    assert resp.status_code == 400
    assert "retention window" in resp.get_json()["error"]["message"]


def test_unknown_sources_are_still_rejected(app, auth, now) -> None:
    resp = _put(app, auth, _snapshot(now), source="nonsense")
    assert resp.status_code == 400
    assert resp.get_json()["error"]["code"] == "unsupported_personal_data_source"


def test_host_source_still_validates_its_own_shape(app, auth, now) -> None:
    fridge = {
        "version": "personal_data_bridge_v1",
        "source_id": "reminders.fridge",
        "generated_at": _iso(now),
        "expires_at": _iso(now + timedelta(hours=12)),
        "data": {
            "items": [
                {
                    "id": "reminder-yogurt",
                    "title": "Yogurt",
                    "due_date": "2026-08-02",
                    "priority": "high",
                    "completed": False,
                }
            ]
        },
    }
    assert _put(app, auth, fridge, source="reminders.fridge").status_code == 200
    assert _put(app, auth, _snapshot(now), source="reminders.fridge").status_code == 400


def test_delete_drops_the_snapshot(app, auth, now) -> None:
    assert _put(app, auth, _snapshot(now)).status_code == 200
    assert (
        app.test_client().delete("/api/app/v1/personal-data/signals", headers=auth).status_code
        == 204
    )
    with app.app_context():
        assert app.config["PERSONAL_DATA_STORE"].publications("signals") == []
