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

## How it works

Two module attributes on `app.companion_api`, both read at request time rather
than at import, so nothing needs re-registering:

- `PERSONAL_DATA_SOURCES` — the guard in `put_personal_data` and the
  `personal_data.sources` list in the capability probe.
- `_validate_reminders_fridge` — the `else` arm of the validator dispatch. Both
  host validators share a `(source_id, body)` signature, so ours wraps it and
  delegates anything that isn't `signals`.

Everything else is inherited untouched: pairing and scoped bearer auth,
latest-only-per-publisher storage, out-of-order and conflict rejection, expiry
tombstones, and the data-change refresh event (the host already emits a
whole-source event for source ids it doesn't recognise).

If a host release renames either attribute, the patch logs a warning and does
nothing — the bridge keeps working exactly as it shipped, minus the `signals`
source. That's the failure mode to expect on an upgrade, and the reason the test
suite runs against a real app rather than a mock.

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

## Tests

Against a real Tesserae app, with the plugin loaded from a throwaway data root:

```sh
TESSERAE_ROOT=~/Projects/TiagoJMartins/tesserae \
  ~/Projects/TiagoJMartins/tesserae/.venv/bin/python -m pytest signals_core/ -q
```

## Status

The display widget isn't here yet — this repo currently ships the bridge only,
so it installs through the authored-plugin path rather than the catalog (the
catalog's `kind` enum has no `data`; a data plugin reaches it as part of a
bundle). The widget lands as `signals_stat`, and the catalog entry becomes one
bundle with `folders: ["signals_core", "signals_stat"]`.

## License

AGPL-3.0-or-later, matching Tesserae.
