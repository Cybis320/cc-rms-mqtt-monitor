# Bridge contract — MQTT → ntfy / Telegram

Instructions for the broker-side alert bridge. The station monitor
(`cc-rms-mqtt-monitor`) only *publishes retained JSON state*; this bridge turns
that into ntfy/Telegram notifications. Topic/namespace: everything is under
`stations/#` (the broker ACL only allows that tree). Alerts fan out to a set of
**handles** (§3); a handle is the Telegram token and, on ntfy, the topic name
with an optional configurable prefix (`ntfy.topic_prefix`) — **empty in the
contrailcast deployment**, so the handle is identical on both channels.

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
- `capture_backend` (`gst`|`cv2`|`null`) + `media_backend` (configured) — the
  actual vs configured capture backend; a `gst`-configured station running `cv2`
  has silently fallen back (see the `backend_fallback` problem).
- `disconnects_session` + `watchdog_restarts_session` (int|null) — stream
  instability counters since the current day/night capture session began (reset
  at each day<->night transition): unplanned stream drops that forced a reconnect,
  and RMS capture-watchdog restarts. Informational; 0 on a healthy stream.
- `meteors_session` (int|null) — meteors detected so far this session (live
  running total, resets at each day<->night transition; 0 by day). Matches RMS's
  end-of-night `TOTAL`. For a live meteor rate / flux indicator on the dashboard
  (true flux also needs collecting area + limiting magnitude, which aren't live).
- `stars_recent` (int|null) — the most recent per-FF "Detected stars" count. NOT
  accumulated (unlike `meteors_session`): it's an instantaneous sky-transparency /
  limiting-magnitude reading — high on a clear night, low/0 when clouded or by day.
  `null` until the first FF is processed. Informational, for the dashboard.
- `rms_mode` — RMS's **actual** day/night capture mode: `day` | `night` | `null`
  (unknown — no recent watchdog line, e.g. capture down). Ground truth from RMS's
  in-process `daytime_mode` flag, not the sun. Informational, for the dashboard.
- `camera_standby` (bool) — the camera has been unpingable for a sustained window
  **while output was stalled**, so the monitor has collapsed this record to a
  single `camera_unreachable` problem (in `problems[]`, severity `error`) and
  suppressed the whole downstream cascade — including `capture_down`. When
  `true`, **that one problem is the story**; don't infer additional faults from
  the sparse record (most other metrics are omitted in standby). It stays `true`
  every cycle until the camera answers again, so treat entry/exit as the alert
  and all-clear and don't re-notify while it holds. `camera_unreachable_s` (float,
  present in standby) is how long it's been unreachable. Absent/`false` on healthy
  stations. The collapse is done in the monitor (not here) so the dashboard sees
  the clean record too — the bridge just routes the `camera_unreachable` problem
  like any other.
- `rms_branch` — RMS git branch the checkout is on (host-wide).
- `rms_remote` — URL of the repo the RMS checkout pulls from (origin; host-wide;
  URL-embedded credentials stripped).
- `rms_up_to_date` (bool) — HEAD is exactly the live remote tip (ls-remote vs HEAD).
- `rms_head` (string) — the checkout's current commit SHA, from a purely **local**
  `git rev-parse HEAD` (**no fetch, no pull, not even an ls-remote** — zero network
  on the station; host-wide). Lets the box compute staleness **exactly**: the box
  watches the branch ref with its own `ls-remote` polling and measures "behind" as
  `now − when the tip first moved off this exact `rms_head``. Immune to (a)
  commit-date/merge quirks and (b) how often — or whether — the *station* re-checks:
  a station can be wrong about `rms_up_to_date` (stale) and the box still gets it
  right, because it needs only the local HEAD sha. Absent on detached HEAD / no
  checkout. **Where the box computes staleness this way it is preferred over
  `rms_out_of_date_days`** below, which is bounded by the monitor's own re-check
  cadence (a station that notices late under-reports).
- `rms_out_of_date_days` (float) — how long the checkout has been behind: now minus
  when the monitor **first observed this HEAD to be behind its remote tip** (i.e.
  when the branch ref first moved past it). `0.0` while up to date. This is NOT a
  commit-date lag (a commit's date can predate when it lands on the branch, so a
  commit-date "days behind" jumps the instant an old-dated PR merges); the stamp is
  the wall-clock moment the ref advanced, observed by polling and persisted across
  restarts (resolution ~= the poll interval). Host-wide (one checkout per box),
  mirrored onto each station record. **See §9 for the alert tiers.** Absent when
  up_to_date is undeterminable (offline / detached / no upstream).
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

For **each station alert**, publish the notification to these handles (each is
an ntfy topic — prefixed by `ntfy.topic_prefix` if set — and a Telegram match):

- `<group_slug>`                        — the operator's group
- **`<P>` for every leading prefix P of `station_id` with `len(P) >= 3`** —
  `USC`, `USC0`, … up to `<full station_id>`

Prefixes shorter than 3 chars (`U`, `US`) are **deliberately skipped**:
they'd each receive *every* station's alerts.

The prefix expansion is what lets a coordinator **subscribe to `USC`
(or `CAC`, `USL`) once** and automatically receive every current *and
future* station whose ID starts with that prefix — no action when a new station
deploys. Keep the expansion from 3 chars up to the full ID (don't collapse it to
only the full ID, or `USC`-style network subscriptions stop working).

> The network codes (`USC`, `CAC`, `USL`, `USV`, …) are all 3 chars, so the
> 3-char floor covers every one of them.

For **each host alert** (and the offline Last-Will), fan out to:
- `<group_slug>` for each entry in `group_slugs`
- `<P>` for every prefix P (`len >= 3`) of each entry in `station_ids`

Station IDs are alphanumeric and `group_slug` is pre-slugified, so every handle
is a valid ntfy topic / Telegram token (no spaces).

### 3b. Category axis (a second, orthogonal set of handles)

The handles above are the **station axis** (*whose* station). A bridge MAY also
fan out on a **category axis** (*what kind* of problem), letting a subscriber
follow one problem class network-wide, across all stations:

- The bridge classifies each alert from its `problems[]` text against a
  configurable map `categories: {name: [regex, …]}` (case-insensitive). For every
  category whose pattern matches, it **also** publishes the alert to the handle
  `<category>` — e.g. `code` (crashes, tracebacks, RMS `-ERROR-` log lines).
  Symptoms that merely *result* from a fault (e.g. *capture process not running*,
  *timelapse not generated*) carry no category — the underlying error surfaces its
  own alert, which is what lands in `code`, so there's no double-tagging.
  > Startup/build crashes now reach `code` too: an import or Cython-build failure
  > that dies before RMS logging starts is scanned from the capture's systemd
  > journal and surfaced as a normal `log_fatal` problem (*"Fatal error in log
  > (Nx): …ImportError…"*). It carries an optional `fatal_source: "journal"` on
  > the record for provenance; no bridge change is needed — it classifies as
  > `code` by its `problems[]` text like any other fatal.
- If `category_repo_scope` is on, each category alert is **also** published to
  `<category>-<repo>`, where `<repo>` is a short handle for the station's
  `rms_remote` (the official RMS → `upstream`; a fork → its owner, or a
  `repo_handles` alias). This lets a developer follow, say, `code-upstream` and
  `code-<their-fork>` while ignoring other forks. Requires the monitor to publish
  **`rms_remote`** (its `origin` URL) in the health record; absent it, only the
  unscoped `<category>` handle is used.

The category axis is **additive** — it never replaces the station handles, so an
operator subscribed to their `group_slug`/prefix still receives every alert
(code ones included) for their own stations. The matching recovery/all-clear is
fanned out to the same category handles as the alert.

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
  starts with the token (prefix match — same `USC` semantics).
- commands: `/subscribe <token>`, `/unsubscribe <token>`, `/list`.
- prefix tokens (e.g. `USC`) give the same "subscribe once, future stations
  auto-covered" behavior as ntfy.

## 7. Test alerts

**A `test: true` record is NOT a special routing case.** Route it through the
exact same fan-out as a real record *of the same kind* (§3) — classify it as a
station vs host record by the §1 rule (`station_ids` present ⇒ host record), then
fan out on the same axes. The *only* difference from a real alert is: **don't**
persist it in your notify-on-change state (a `<…>-TEST`/burst id never recovers),
and optionally tag it as a test / lower priority. Do **not** branch test records
onto a narrower path — if you only read a singular `group_slug`/`station_id` for
tests, a host test won't fan out to `group_slugs`/`station_ids` and you'll see
"the test didn't fan out like a real message."

- `cc-rms-monitor --test` → a one-off, **non-retained** *station* record
  (`status: degraded`, a `<station>-TEST` id, the host's real `group_slug`).
  Fans out on the **station** axes (§3): `<group_slug>` + `<prefix>`.
- `cc-rms-monitor --test-udp [RATE]` → a one-off, **non-retained** *host* record
  on `stations/<host>/health` with a real `udp_rcvbuf_errors` problem (simulated
  `udp_rcvbuf_errors_per_min`, default 999) and the host's real
  `group_slugs`/`station_ids`. Fans out on the **host** axes (§3):
  `<group_slug>` for each `group_slugs` entry + `<prefix>` for each
  `station_ids`. The real retained host record is left untouched.

## 8. Notes

- Open/anonymous broker, plaintext 1883, `stations/#` only.
- Treat a station as stale if its host's `status` is `offline` or its
  `timestamp` is older than ~3× the publish interval.
- Don't alert on `status: ok` except as a recovery edge.

## 9. Stale-RMS alert (bridge-side)

This is a separate axis from the operational `status` (a fully-working box can
still be running stale code). The **bridge** decides severity, on the **host**
record (one RMS checkout per box — don't multiply by each station).

**Gate on `rms_up_to_date == false`, then tier on `rms_out_of_date_days`** (how
long the checkout has been behind — measured from when the remote ref first moved
past this HEAD, immune to commit-date quirks):

| Condition | Action |
|---|---|
| `rms_up_to_date == true` | **no alert** — on the tip (regardless of age; a quiet branch just hasn't moved) |
| not up-to-date, `rms_out_of_date_days < 1` | **no alert** — just fell behind; the nightly updater gets a chance to catch up |
| not up-to-date, `1 ≤ days ≤ 3` | **degraded** — behind for 1–3 days; updater hasn't caught up |
| not up-to-date, `days > 3` | **error** — updater appears stuck |
| `rms_up_to_date` absent | no alert (couldn't determine — offline/detached) |

> Do **not** tier on a commit-date "days behind": a commit's date can long
> predate when it merges, so that misreports a huge lag the instant such a commit
> lands. `rms_out_of_date_days` counts from when the ref actually moved past this
> station's HEAD, which is what tells you how long the updater has been stuck.

Fan it out on the host axes (§3, `group_slugs` + `station_ids`). `rms_up_to_date`
is the at-a-glance boolean for the dashboard. Suppress while `maintenance` is
true (§4), same as any host alert.
