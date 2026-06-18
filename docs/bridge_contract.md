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
- `maintenance` (bool) + `maintenance_reason` — expected-disruption flag (see §4)
- `lat`, `lon` — approximate station coordinates (decimal degrees, rounded to
  ~1 km); present only when the RMS `.config` has coordinates. For dashboard maps.
- `alt_centre`, `az_centre` — camera pointing (centre of field), whole degrees,
  from the station's platepar; present only when the platepar is readable.
- `host`, `timestamp` (ISO-8601 UTC)

**Host** (`stations/<host>/health`):
- `status`, `problems` (OOM / memory / UDP RcvbufErrors)
- `station_ids` — list of cameras on that host
- `groups`, `group_slugs` — distinct groups on that host
- `host`, `timestamp`
- *UDP-only (present when a station uses `protocol: udp`):* `udp_rcvbuf_errors`
  (cumulative), `udp_rcvbuf_errors_per_min` (growth rate — the alert signal),
  `udp_rcvbuf_error_pct`, `udp_in_datagrams`, `udp_rmem_max`. A
  `udp_rcvbuf_errors` problem is host-wide; route it like any host alert (§3).

**Last-Will** (`stations/<host>/status`): `offline` ⇒ that host's agent/host is
down → all its stations are stale. Use the host's last `health` record (its
`station_ids`/`group_slugs`) to route the offline alert.

## 3. Fan-out axes (the important part)

For **each station alert**, publish the notification to these ntfy topics
(and match them in Telegram):

- `cc-<group_slug>`                     — the operator's group
- **`cc-<P>` for every leading prefix P of `station_id` with `len(P) >= 3`** —
  `cc-USC`, `cc-USC0`, … up to `cc-<full station_id>`

Prefixes shorter than 3 chars (`cc-U`, `cc-US`) are **deliberately skipped**:
they'd each receive *every* station's alerts and hit ntfy rate limits.

The prefix expansion is what lets a coordinator **subscribe to `cc-USC`
(or `cc-CAC`, `cc-USL`) once** and automatically receive every current *and
future* station whose ID starts with that prefix — no action when a new station
deploys. Keep the expansion from 3 chars up to the full ID (don't collapse it to
only the full ID, or `cc-USC`-style network subscriptions stop working).

> The network codes (`USC`, `CAC`, `USL`, `USV`, …) are all 3 chars, so the
> 3-char floor covers every one of them.

For **each host alert** (and the offline Last-Will), fan out to:
- `cc-<group_slug>` for each entry in `group_slugs`
- `cc-<P>` for every prefix P (`len >= 3`) of each entry in `station_ids`

Station IDs are alphanumeric and `group_slug` is pre-slugified, so every `cc-<…>`
is a valid ntfy topic / Telegram tag (no spaces).

## 4. Don't alert on EXPECTED disruption (use the monitor's `maintenance` flag)

The monitor reports the true *current* state every 60 s, and it also tells you
**when disruption is expected** — it has the local knowledge the bridge doesn't.
Every record carries:

- `maintenance` — `true` when this host is in a known-disruption state
- `maintenance_reason` — `"booting"` (host just rebooted) or `"rms-updating"`
  (a GRMSUpdater process is actually running). Self-healing on the station side:
  a lingering lock/flag file alone never sets this, and it clears on boot / when
  no updater is alive / after the update window — so it can't get stuck `true`.

Rules:

1. **Suppress while `maintenance` is true.** A `degraded`/`error` record with
   `maintenance: true` is the nightly `GRMSUpdater` cron restarting capture or
   the box rebooting — **do not notify**. When `maintenance` is false, a real
   failure → notify (no artificial delay).
2. **Offline during maintenance is expected.** If a host's last record before an
   `offline` Last-Will had `maintenance: true` (e.g. `rms-updating`), or it comes
   back reporting `maintenance: "booting"`, treat the `offline`/`online` flap as
   an expected reboot and stay quiet.
3. **Only notify on change**, and **recovery only if you alerted** (don't send a
   `→ ok` for an episode you suppressed).
4. **Short backstop (optional):** a small "must persist ~2–3 min" window catches
   the rare transient the monitor didn't classify, without delaying real alerts
   much. The `maintenance` flag is the primary mechanism; the window is just a
   safety net.

This keeps real failures instant while the ~19:00 update/reboot churn (and RMS's
own watchdog restarts) is silenced by the station that actually knows it's
expected.

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

## 7. Test alerts

`cc-rms-monitor --test` publishes a one-off, **non-retained** station record with
`"test": true`, `status: "degraded"`, a `<station>-TEST` id, and the host's real
`group_slug`. Route it exactly like a normal alert (so the operator confirms the
chain) — optionally tag it as a test / lower priority, and **don't** persist it
in your notify-on-change state (the id never recovers).

`cc-rms-monitor --test-udp [RATE]` publishes a one-off, **non-retained**
*host* record (on `stations/<host>/health`) with `"test": true` and a real
`udp_rcvbuf_errors` problem (simulated `udp_rcvbuf_errors_per_min`, default 999),
carrying the host's real `group_slugs`/`station_ids`. Use it to exercise the
**host** alert fan-out (§3). Same rule: it's non-retained and `test: true`, so
don't persist it — the real retained host record is left untouched.

## 8. Notes

- Open/anonymous broker, plaintext 1883, `stations/#` only.
- Treat a station as stale if its host's `status` is `offline` or its
  `timestamp` is older than ~3× the publish interval.
- Don't alert on `status: ok` except as a recovery edge.
