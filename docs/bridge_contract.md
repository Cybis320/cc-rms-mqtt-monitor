# Bridge contract — MQTT → ntfy / Telegram

Instructions for the broker-side alert bridge. The station monitor
(`cc-rms-mqtt-monitor`) only *publishes retained JSON state*; this bridge turns
that into ntfy/Telegram notifications. Topic/namespace: everything is under
`stations/#` (the broker ACL only allows that tree). Alert topics use the
`cc-` prefix.

## 1. Subscribe (input from the monitor)

```
stations/+/health     retained JSON — one per camera AND one per host
stations/+/status     retained "online" / "offline"  (host Last-Will)
```

Distinguish the two `health` shapes by payload:
- **station record** has `station_id` (+ `group`, `group_slug`, capture metrics).
- **host record** has `station_ids` + `mem_available_mb`/`oom_kill_count` (no `station_id`).

## 2. Payload fields you need

**Station** (`stations/<station_id>/health`):
- `station_id` — e.g. `US005A`
- `status` — `ok` | `degraded` | `error`
- `problems` — list[str], human-readable; **this is the notification body**
- `group` — human label (e.g. `Elginfield Contrail Cameras`)
- `group_slug` — slugified handle (e.g. `Elginfield-Contrail-Cameras`); valid ntfy topic
- `host`, `timestamp` (ISO-8601 UTC)

**Host** (`stations/<host>/health`):
- `status`, `problems` (OOM / memory)
- `station_ids` — list of cameras on that host
- `groups`, `group_slugs` — distinct groups on that host
- `host`, `timestamp`

**Last-Will** (`stations/<host>/status`): `offline` ⇒ that host's agent/host is
down → all its stations are stale. Use the host's last `health` record (its
`station_ids`/`group_slugs`) to route the offline alert.

## 3. Fan-out axes (the important part)

For **each station alert**, publish the notification to ALL of these ntfy topics
(and match them in Telegram):

- `cc-<group_slug>`                     — the operator's group
- **`cc-<P>` for every leading prefix P of `station_id`** — `cc-U`, `cc-US`,
  `cc-USC`, `cc-USC0`, … `cc-<full station_id>`

The prefix expansion is what lets a coordinator **subscribe to `cc-USC`
(or `cc-CAC`, `cc-UV`, `cc-USL`) once** and automatically receive every current
*and future* station whose ID starts with that prefix — no action when a new
station deploys, because its alerts are published to all its prefixes as soon as
its monitor comes online. **This auto-coverage requires the full prefix
expansion — do not shorten it to just the full ID.**

For **each host alert** (and the offline Last-Will), fan out to:
- `cc-<group_slug>` for each entry in `group_slugs`
- `cc-<P>` for every prefix P of each entry in `station_ids`

Station IDs are alphanumeric and `group_slug` is pre-slugified, so every `cc-<…>`
is a valid ntfy topic / Telegram tag (no spaces).

## 4. Notify on change, not every cycle

The monitor re-publishes retained state **every 60 s**. Keep the last seen
`status` (and `problems`) per `station_id`/host and only notify when it
**changes** — entering `degraded`/`error`, gaining new `problems`, or recovering
to `ok`. Otherwise you'll send a notification every minute.

## 5. Notification mapping

- **title:** `"<station_id> — <status>"` (or `"<host> (host) — <status>"`)
- **body:** `"\n".join(problems)`
- **severity:** `error` → high priority + 🔴 tag; `degraded` → default + 🟠;
  recovery to `ok` → low + ✅
- ntfy: set via `X-Title`, `X-Priority`, `X-Tags` headers (or JSON publish).

## 6. Telegram (no wildcards — bridge holds the rules)

Telegram can't subscribe by topic, so the bot keeps `chat_id → {tokens}` and
matches each alert:
- a token matches if it equals the alert's `group_slug`, **or** the `station_id`
  starts with the token (prefix match — same `cc-USC` semantics).
- commands: `/subscribe <token>`, `/unsubscribe <token>`, `/list`.
- prefix tokens (e.g. `USC`) give the same "subscribe once, future stations
  auto-covered" behavior as ntfy.

## 7. Notes

- Open/anonymous broker, plaintext 1883, `stations/#` only.
- Treat a station as stale if its host's `status` is `offline` or its
  `timestamp` is older than ~3× the publish interval.
- Don't alert on `status: ok` except as a recovery edge.
