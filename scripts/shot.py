"""Screenshot signals_stat from a running dev server, at every size.

Seeds a demo snapshot into the dev server's personal-data store first (through
the store class, so the on-disk shape is whatever the host currently writes),
then drives ``/_test/render`` with Playwright. ``/_test/render`` is
loopback-exempt from the auth gate, so no login is needed.

    mise run serve            # in one shell
    mise run shot --sizes lg,md,sm,xs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESSERAE_ROOT = Path(
    os.environ.get("TESSERAE_ROOT", str(Path.home() / "Projects/TiagoJMartins/tesserae"))
).expanduser()
sys.path.insert(0, str(TESSERAE_ROOT))

from app.state.personal_data_store import PersonalDataSnapshotStore  # noqa: E402

# What the shot shows: a beacon mid-alert, because that's the state the widget
# exists for. The other rows make the picker plausible in a screenshot of the
# editor and give the value fragment something numeric to render.
DEMO_ROWS = [
    {"id": "slack_unread", "label": "Slack", "value": "on", "unit": None, "state": "alert"},
    {"id": "inbox", "label": "Inbox", "value": 12, "unit": "msgs", "state": "ok"},
    {"id": "vpn", "label": "VPN", "value": "off", "unit": None, "state": "ok"},
]
DEMO_OPTIONS = {
    "row_id": "slack_unread",
    "title": "Slack",
    "on_values": "on,true,1,alert",
    "on_text": "Unread",
    "off_text": "Clear",
    "accent": "accent-1",
    "show_publisher": True,
}
VARIANTS = {
    # (row value, row state, how long ago it was sampled, output basename)
    "on": ("on", "alert", 0, None),
    "off": ("off", "ok", 0, "extra-1"),
    "stale": ("on", "alert", 3 * 3600, "extra-2"),
}


def seed(data_root: Path, variant: str) -> None:
    value, state, age_s, _ = VARIANTS[variant]
    store = PersonalDataSnapshotStore(data_root / "core" / "companion_personal_data.json")
    now = datetime.now(UTC).replace(microsecond=0)
    sampled = now - timedelta(seconds=age_s)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    rows = [{**row, "at": sampled.strftime(fmt)} for row in DEMO_ROWS]
    rows[0] = {**rows[0], "value": value, "state": state}
    store.put(
        "signals",
        snapshot={
            "version": "personal_data_bridge_v1",
            "source_id": "signals",
            "generated_at": sampled.strftime(fmt),
            "expires_at": (sampled + timedelta(hours=12)).strftime(fmt),
            "data": {"rows": rows},
        },
        generated_epoch=sampled.timestamp(),
        expires_epoch=(sampled + timedelta(hours=12)).timestamp(),
        publisher_id="companion_demo",
        publisher_name="Kitchen Mac",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="lg", help="comma-separated cell sizes")
    parser.add_argument("--fragment", default="full")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    # Screenshots belong to the catalog (its schema declares them and its
    # validator checks them), so write straight into a sibling catalog checkout
    # when there is one rather than keeping a second copy here.
    catalog = REPO.parent / "tesserae-widgets" / "screenshots"
    parser.add_argument("--out", default=str(catalog if catalog.is_dir() else REPO / "screenshots"))
    parser.add_argument(
        "--data-root", default=os.environ.get("SIGNALS_DATA_ROOT", str(REPO / ".dev-data"))
    )
    parser.add_argument(
        "--variant",
        default="on",
        choices=sorted(VARIANTS),
        help="which state to seed: on (primary shots), off / stale (carousel extras)",
    )
    args = parser.parse_args()

    data_root = Path(args.data_root)
    if not data_root.exists():
        print(f"no data root at {data_root}; start 'mise run serve' first")
        return 2
    seed(data_root, args.variant)

    from playwright.sync_api import sync_playwright

    # Cell dims per size token, matching app/composer.py's SIZE_DIMENSIONS: the
    # viewport has to be the cell, or the shot carries page chrome around it.
    dims = {"xs": (180, 180), "sm": (380, 240), "md": (640, 400), "lg": (1200, 800)}
    # Screenshots are keyed by CATALOG id, not plugin id: the catalog entry that
    # ships this bundle is "signals", and its validator looks there.
    out_dir = Path(args.out) / "signals"
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for size in [s.strip() for s in args.sizes.split(",") if s.strip()]:
                if size not in dims:
                    print(f"unknown size {size!r}")
                    return 2
                width, height = dims[size]
                query = urllib.parse.urlencode(
                    {
                        "plugin": "signals_stat",
                        "size": size,
                        "fragment": args.fragment,
                        "fresh": "1",
                        "opts": json.dumps(DEMO_OPTIONS),
                    }
                )
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(f"{args.base_url}/_test/render?{query}", wait_until="networkidle")
                # Fonts and the shadow-DOM mount land a frame after networkidle;
                # the host's own renderer waits the same way.
                page.wait_for_timeout(700)
                basename = VARIANTS[args.variant][3] or size
                dest = out_dir / f"{basename}.png"
                page.screenshot(path=str(dest))
                page.close()
                written.append(dest)
                time.sleep(0.1)
        finally:
            browser.close()

    for path in written:
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
