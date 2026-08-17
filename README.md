# Signals for Tesserae

Push labelled state to an e-ink panel from anywhere that can make an HTTP
request — an iOS Shortcut, a shell script, a Hammerspoon timer, a Home Assistant
automation — without standing up an endpoint for a widget to poll.

Tesserae already has the right channel for this: the Companion bridge, where a
paired client PUTs an expiring snapshot and a widget reads it back with no
network access of its own. What it doesn't have is a *generic* source — the
host's allowlist is `("reminders", "reminders.fridge")`, validated per source.
`signals_core` adds one.

## Folders shipped

- `signals_core` — `kind: "data"`. Registers the `signals` personal-data source.
  No cell of its own.
- `signals_stat` — `kind: "widget"`. Renders one published row as a hero value
  or a two-state beacon.

Installed together as one catalog bundle; the widget is useless without the
source, and the source has nothing to show without the widget.

## The widget

Pick a signal (the picker lists every published row, prefixed with the
publisher when more than one machine publishes), then choose how it reads:

- **Value** — the row's value as a hero number or string, with its unit.
- **Beacon** — two states with your own icon and wording per state. `Values
  that count as ON` is matched against the row's `state` first and its `value`
  second, so a publisher can name states (`alert` / `ok`) or just send a value.
  Auto mode picks beacon whenever ON values are configured.

Emphasis scales with the cell: at `xs`/`sm` an ON beacon flips the whole tile to
the accent fill (contrast, which survives a 1-bit dither, rather than hue), and
at `xs` it drops to the glyph alone rather than truncating a word. At `md`/`lg`
it uses the soft accent wash so a half-panel cell doesn't shout over its
neighbours.

Three states, not two: `Treat as stale after` seconds (measured against the
row's own timestamp when the publisher sent one) turns a quiet publisher into a
visible "No update, last seen 3h ago" rather than a confident OFF. Past the
server's 48 h expiry the cell says the snapshot expired; a row that vanished
from a live snapshot reads as missing, and the widget never falls back to a
different row under the old title.

Fragments for the Panels canvas: `full`, `value`, `badge`.

## How it works

Three module attributes on `app.companion_api`, all read at request time rather
than at import, so nothing needs re-registering:

- `PERSONAL_DATA_SOURCES` — the guard in `put_personal_data` and the
  `personal_data.sources` list in the capability probe.
- `_validate_reminders_fridge` — the `else` arm of the validator dispatch. Both
  host validators share a `(source_id, body)` signature, so ours wraps it and
  delegates anything that isn't `signals`.
- `_valid_client` — pairing hard-rejects any platform but `ios`. A Mac or a Pi
  publishing its own state shouldn't have to claim to be an iPhone, so
  `macos`, `linux`, `shortcuts` and `homeassistant` are accepted too and the
  rest of the host's client validation is reused as-is.

Everything else is inherited untouched: pairing and scoped bearer auth,
latest-only-per-publisher storage, out-of-order and conflict rejection, expiry
tombstones, and the data-change refresh event (the host already emits a
whole-source event for source ids it doesn't recognise).

If a host release renames any of them, that part of the patch logs a warning and
does nothing — the bridge keeps working exactly as it shipped, minus the
`signals` source (or minus non-iOS pairing). That's the failure mode to expect
on an upgrade, and the reason the test suite runs against a real app rather than
a mock.

## Publishing a snapshot

Pair once: Settings → Companion → issue a code, then exchange it.

```sh
TOKEN=$(curl -sS -X POST "$TESSERAE/api/app/v1/pair" \
  -H 'Content-Type: application/json' \
  -d '{"code":"<pairing code>","client":{"name":"MacBook","platform":"macos",
       "app_version":"0.1.0","installation_id":"<uuid>"}}' | jq -r .token)
```

Then PUT rows whenever the state changes:

```sh
now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
exp=$(date -u -v+12H +%Y-%m-%dT%H:%M:%SZ)   # GNU date: -d '+12 hours'

curl -sS -X PUT "$TESSERAE/api/app/v1/personal-data/signals" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq -nc --arg now "$now" --arg exp "$exp" '{
        version: "personal_data_bridge_v1",
        source_id: "signals",
        generated_at: $now,
        expires_at: $exp,
        data: { rows: [
          { id: "slack_unread", label: "Slack", value: "on",
            unit: null, state: "alert", at: $now }
        ]}
      }')"
```

`GET /api/app/v1/personal-data/status` returns freshness metadata only;
`DELETE /api/app/v1/personal-data/signals` drops the snapshot.

## Snapshot schema

Envelope is the host's: `version` must be `personal_data_bridge_v1`, `source_id`
must match the path, `generated_at` / `expires_at` are ISO 8601 with
`expires_at > generated_at` and a TTL of at most 48 h (the host treats a
snapshot as stale after 24 h). `data` carries exactly one key, `rows`.

| field   | required | shape                                            |
| ------- | -------- | ------------------------------------------------ |
| `id`    | yes      | 1–128 chars, unique within the snapshot          |
| `label` | yes      | 1–128 chars, what a widget prints                |
| `value` | yes      | string ≤256 chars, number, boolean, or null      |
| `unit`  | no       | string ≤16 chars, or null                        |
| `state` | no       | string ≤32 chars, or null — your own state name  |
| `at`    | no       | ISO 8601, when the publisher sampled it, or null |

At most 64 rows, and the array may not be empty. The schema is deliberately
bounded: whatever is accepted sits in the store in the clear until it expires.

`state` is free-form on purpose — the publisher names the state (`alert`, `ok`,
`degraded`) and the widget maps names to icons and tone, so a new state doesn't
need a server change.

## Publisher identity and retention

The server derives an opaque publisher id from each pairing, and keeps exactly
one snapshot per `(publisher, source)`, replaced on each accepted PUT. Several
machines can publish independently; a widget picking rows can tell them apart.
Nothing is retained past `expires_at` beyond a timestamp tombstone, which is
what lets a panel distinguish "expired" from "never synced".

## Development

Everything runs against a local Tesserae checkout, because the plugin contract
is the host's and the only honest harness is the host itself. `TESSERAE_ROOT` in
`mise.toml` points at it.

```sh
mise run test                    # both folders' tests, against a real app
mise run serve                   # dev server, this bundle loaded from ./.dev-data
mise run shot --sizes lg,md,sm,xs   # screenshots into the sibling catalog checkout
mise run release 0.1.0           # tag, push, print tarball URL + sha256
```

`mise run shot` seeds a demo snapshot into the dev server's store and drives
`/_test/render`, which is loopback-exempt from the auth gate. `--variant off`
and `--variant stale` produce the catalog's carousel extras.

## Install

One catalog entry installs both folders. It's published through
[TiagoJMartins/tesserae-widgets](https://github.com/TiagoJMartins/tesserae-widgets):
point a server's **Marketplace catalog URL** at that index, then Settings →
Widgets → Browse → Install. A restart loads `signals_core`, which is when the
`signals` source starts being advertised.

## License

AGPL-3.0-or-later, matching Tesserae.
