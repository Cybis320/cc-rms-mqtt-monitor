# CC RMS MQTT Monitor

A standalone, host-level agent that monitors the health of every
[RMS](https://github.com/CroatianMeteorNetwork/RMS) meteor-camera station on a
machine and publishes it to an MQTT broker as one retained plain-JSON blob per
station (plus a host/OS blob). A broker-side ntfy + Telegram bridge consumes
those topics for alerting, and they're equally usable by any custom dashboard.

It supports both deployment schemes automatically:

- **Multicam** (many stations on one box): one station per `~/source/Stations/*/.config`.
- **Single-cam**: the station defined by `~/source/RMS/.config` (used only when
  no multicam stations are found).

## What it monitors

Per station, each cycle (default 60 s):

| Signal | What it catches |
|---|---|
| **Capture process alive** | `RMS.StartCapture` for that station's config is gone |
| **Mode-aware freshness** | knows from `.config` (`continuous_capture`, `switch_camera_modes`, `save_frames`) + computed **sun elevation** whether to expect `FF_*.fits` (night) or `FramesFiles/*_d.jpg` frame images (day); flags the *right* output going stale and never false-alarms on the idle pipeline |
| **Silent pipeline failure** | capturing fine but **no `FTPdetectinfo`/`CALSTARS`** produced — the general case of "a missing `.so` / import error broke detection but capture looks alive" |
| **Fatal log errors** | scans the live log for `Traceback`, `ImportError`, `ModuleNotFoundError`, `cannot open shared object file`, `Segmentation fault`, etc., and reports the last one |
| **Watchdog events** | RMS's own `WATCHDOG: ... died/stale/Restarting` lines |
| **Dropped frames / buffer fill** | from the periodic `Buffer fill: …%` log line |
| **Disk free** | data partition approaching full |
| **Upload backlog** | `FILES_TO_UPLOAD.inf` queue length |
| **Time sync** | `clock_synchronized` / clock uncertainty from the night summary |
| **Process memory, FITS counts, code version** | from `/proc` and the observation summary |

The combined verdict per station is `ok` / `degraded` / `error`, plus a
human-readable `problems` list.

### Host-level (OS) monitoring

In addition to per-station health, each cycle publishes one **host** record:

| Signal | What it catches |
|---|---|
| **OOM-killer events** | scans the kernel log (`journalctl -k` → `dmesg` → log files) for `Out of memory: Killed process` / `oom-kill:`, reports the count and last victim. A killed `python` (RMS) process is an `error`. |
| **Memory headroom** | `MemAvailable` / `SwapFree` from `/proc/meminfo` — early warning before the OOM-killer fires |
| **Uptime** | host uptime |

The agent **protects itself from the OOM-killer** so it survives to report the
event that kills an RMS process: the systemd unit sets `OOMScoreAdjust=-900`
(applied with privilege), and the loop also best-effort lowers its own
`oom_score_adj` at startup. It stays tiny (**~19 MB RSS**, pure stdlib + paho +
pyyaml) and the unit caps it at `MemoryMax=128M` so it can never itself add to
host memory pressure.

## Health checks (triggers)

Every check has a stable key and is **on by default**. The verdict is the worst
of all fired checks (`ok` < `degraded` < `error`); the `problems[]` list carries
the human-readable text.

| Key | Severity | Fires when | Threshold |
|---|---|---|---|
| `capture_down` | error | the station's `RMS.StartCapture` process isn't running | — |
| `capture_stalled` | error | expected output is stale — no FF (night) or no frame (continuous day) | `output_fresh_error_s` (300s) |
| `detection_stalled` | error | capturing FF but no `FTPdetectinfo`/`CALSTARS` produced | `detection_grace_s` (1800s) |
| `timelapse_missing` | degraded | a completed frame session's ffmpeg failed — its `_frametimes.json` exists but no `_frames_timelapse.mp4` | `timelapse_grace_s` (1h) |
| `timelapse_overdue` | degraded | saving frames but no `_frames_timelapse.mp4` produced in ages (generation not running at all; latitude-independent) | `timelapse_max_age_s` (30h) |
| `log_fatal` | error | `Traceback`/`ImportError`/`cannot open shared object`/segfault in the log | — |
| `watchdog` | degraded | RMS `WATCHDOG: died/stale/Restarting` event | — |
| `disk_low` | degraded / error | data partition free space low / critical | `disk_free_warn_gb` (20) / `disk_free_error_gb` (5) |
| `upload_backlog` | degraded | upload queue length over threshold | `upload_queue_warn` (50) |
| `clock_unsynced` | degraded | last summary reported clock not synchronized | — |
| `clock_uncertainty` | degraded | last summary clock error over threshold | `clock_error_warn_ms` (100) |
| `dropped_frames` | degraded | dropped frames in the last 10 min | `dropped_frames_warn` (10) |
| `oom` | error (python victim) / degraded | host OOM-killer fired (kernel log) | — |
| `host_memory` | degraded / error | host available memory low / critical | `mem_available_warn_mb` (800) / `..._error_mb` (300) |

Day/night for `capture_stalled` comes from the sun (matching RMS's own switch
horizon + programmed delays), not from frame creation. Conditional checks stay
quiet when not applicable (e.g. `upload_backlog` only when uploads are queued,
the clock checks only when a summary exists).

**Silencing a check:** add its key to `disabled_checks` in `config.yaml`
(default empty = all on), e.g.:

```yaml
disabled_checks: [dropped_frames, upload_backlog]
```

Tune sensitivity instead with the `thresholds:` block (see `config.example.yaml`).

## Health topics

```
stations/<host>/status      retained "online"/"offline" (Last Will)
stations/<host>/health       retained JSON host (OS) state blob
stations/<station>/health    retained JSON per-station state blob
```

> **Broker namespace:** the contrailcast broker is an open, unauthenticated,
> plaintext broker whose ACL only permits the `stations/#` topic tree, so every
> topic (including the host status/Last-Will) lives under `stations/`. Alerting
> is handled by a broker-side consumer of the `stations/<id>/health` topics
> (ntfy + Telegram).

The host status topic is the MQTT **Last Will** target, so a crashed agent or
offline host is detected without polling.

Example `health` payload:

```json
{
  "station_id": "US005A",
  "status": "error",
  "problems": ["Detection pipeline produced no output after 2100s of capture",
               "Fatal error in log (3x): ImportError: ... cannot open shared object file"],
  "group": "Elginfield Contrail Cameras",
  "group_slug": "Elginfield-Contrail-Cameras",
  "lat": 43.19,
  "lon": -81.32,
  "capture_alive": true,
  "newest_fits_age_s": 8.2,
  "fits_count": 210,
  "ftpdetect_present": false,
  "fatal_error_count": 3,
  "last_error": "ImportError: .../RMS/Routines/BinImageCy...so: cannot open shared object file",
  "disk_free_gb": 5216.1,
  "upload_queue_len": 0,
  "host": "us005-host",
  "timestamp": "2026-06-15T17:40:00Z"
}
```

`group` is the human-readable label (RMS `camera_group_name`, or the installer's
override); **`group_slug`** is the canonical subscription handle — spaces/
punctuation collapsed to `-` so it is valid as an ntfy topic / Telegram tag.

### Ways to subscribe (ntfy / Telegram)

The bridge fans each alert out to several topics, so you pick the granularity:

| Subscribe to | Covers |
|---|---|
| `cc-<group_slug>` | your whole group (e.g. `cc-Phoenix-1`) |
| `cc-<stationID>` | one camera (e.g. `cc-US005A`) |
| `cc-<prefix>` | **a whole network** — any leading prefix of a station ID **of 3+ characters** (`cc-USC`, `cc-CAC`, `cc-USL`, `cc-USV`, …) |

The prefix axis is the important one for network coordinators: subscribe to
`cc-USC` (or `cc-CAC`, …) **once**, and every current *and future* station whose
ID starts with that prefix is covered automatically — no ntfy change is needed
when a new station is deployed, because the bridge publishes each station's
alerts to all of its ID prefixes (from 3 chars up to the full ID) as soon as its
monitor comes online. Prefixes shorter than 3 chars (`cc-U`, `cc-US`) are not
published — they'd carry every station's traffic and hit ntfy rate limits. The
network codes (`USC`, `CAC`, `USL`, `USV`, …) are all 3 chars, so the floor
covers each of them.

The host record carries `groups` + `group_slugs` + `station_ids`, so host-level
(OOM/memory) alerts fan out to the same group and prefix topics.

## Install

**One command on an RMS station** (clones, installs into `~/vRMS`, seeds
`config.yaml`, installs + starts the hardened systemd service):

```bash
curl -fsSL https://raw.githubusercontent.com/Cybis320/cc-rms-mqtt-monitor/master/scripts/deploy_station.sh | bash
```

It defaults to `mqtt.contrailcast.com:1883` (plaintext) with **no credentials** —
nothing for the operator to configure. The health feed is non-sensitive and
world-readable by design, so TLS is opt-in (see `deploy/README.md`); enable it
only alongside authentication. Override the repo with
`CC_REPO_URL=… bash deploy_station.sh` if you fork it.

> **Opting out:** the monitor honors your RMS `weblog_enable` setting. Any camera
> with `weblog_enable: false` is **not transmitted** to MQTT at all (and a host
> with no opted-in cameras transmits nothing) — same consent flag that controls
> the GMN weblog.

For a manual/dev install:

```bash
./scripts/install.sh          # installs into ~/vRMS, creates config.yaml
```

Edit `config.yaml` only if you need to change defaults (e.g. `tls: false` /
`port: 1883` for a broker without TLS yet) — see `config.example.yaml`.

**Broker operators:** see [`deploy/`](deploy/) for the hardened Mosquitto
config (namespace ACL, resource limits, TLS via Let's Encrypt).

## Auto-update

The installer also sets up a systemd timer that keeps each station current with
the repo. Every 15 minutes it fast-forwards the checkout and, **only if the code
changed**, reinstalls and restarts the service — so a `git push` rolls out to the
fleet within minutes, with no inbound access needed (pull-based, NAT-friendly).

- `scripts/autoupdate.sh` does `git fetch` + `merge --ff-only` (never clobbers
  local commits), then `pip install -e` + `systemctl restart` if HEAD moved.
- Runs as root from `cc-rms-monitor-update.timer` (so it can restart the
  service); git/pip run as the repo owner.

Knobs (env vars at install time):

```bash
CC_NO_AUTOUPDATE=1            # skip installing the timer
CC_UPDATE_INTERVAL=30min     # change the poll interval
CC_BRANCH=stable             # track a release branch instead of master
```

Check / control it:

```bash
systemctl list-timers cc-rms-monitor-update.timer
journalctl -u cc-rms-monitor-update.service     # see what each run did
sudo systemctl start cc-rms-monitor-update.service   # force an update now
sudo systemctl disable --now cc-rms-monitor-update.timer  # stop auto-updates
```

> **Fleet-safety tip:** auto-updating every station to `master` means a bad push
> hits everything. For a real fleet, develop on `master` but point stations at a
> `stable` branch (`CC_BRANCH=stable`) and fast-forward `stable` only when you've
> verified a change — same mechanism, controlled blast radius.

## Usage

```bash
# Local diagnostics, no broker needed — prints each station's health as JSON:
python -m cc_mqtt_monitor --status

# One publish cycle then exit (good for testing the broker connection):
python -m cc_mqtt_monitor --config config.yaml --once

# Run the publish loop (what the systemd service runs):
python -m cc_mqtt_monitor --config config.yaml

# Watch a live multi-station table by subscribing to the broker:
python -m cc_mqtt_monitor --config config.yaml --viewer

# Send one test alert to check the whole broker -> ntfy/Telegram chain:
python -m cc_mqtt_monitor --config config.yaml --test
```

`--test` publishes a single, non-retained, clearly-marked alert carrying this
host's real `group_slug` and a `<station>-TEST` id, so it routes to your normal
subscriptions (`cc-<group_slug>` and the network `cc-<prefix>`). It uses a
separate client id and no Last-Will, so it never disturbs the running service's
status. If you receive it in ntfy/Telegram, the chain works.

Run as a service: see `systemd/cc-rms-monitor.service`.

## Design notes

- **Honors publish consent (`weblog_enable`).** A station whose RMS `.config`
  has `weblog_enable: false` ("show this camera on the GMN weblog") is **never
  published** — not its health, coordinates, pointing, nor its ID/group in the
  host record — and if it had been published before, its retained record is
  **cleared** from the broker. If **no** station on a host consents, the monitor
  **transmits nothing at all**: it doesn't connect/announce, and on opting the
  last station out it wipes the host's status + records and disconnects without
  even an offline marker. In short: set `weblog_enable: false` and that camera
  (or the whole host) is silent on MQTT.
- **Read-only / non-invasive.** Reads `/proc`, files, and logs; it never touches
  RMS processes or data, so it cannot perturb capture.
- **No RMS import.** It parses the `.config` and on-disk artifacts directly, so it
  runs independently of the RMS code version and venv state.
- **Defensive collectors.** A missing directory or down process yields empty
  metrics, never an exception — one broken station never stops the others.

## Requirements

- Python 3.7+
- `paho-mqtt`, `pyyaml` (installed by `scripts/install.sh`)
