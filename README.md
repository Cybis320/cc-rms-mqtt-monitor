# CC RMS MQTT Monitor

A standalone, host-level agent that monitors the health of every
[RMS](https://github.com/CroatianMeteorNetwork/RMS) meteor-camera station on a
machine and publishes it to an MQTT broker as one retained plain-JSON blob per
station (plus a host/OS blob). A broker-side ntfy + Telegram bridge consumes
those topics for alerting, a public read-only dashboard at
**https://status.contrailcast.com** renders the live fleet from the same feed,
and the topics are equally usable by any custom dashboard.

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
| **Dropped frames / buffer fill** | from the periodic `Buffer fill: …%` log line — and, when frames drop, an attributed **`drop_cause`** (see [Dropped-frame attribution](#dropped-frame-attribution)) |
| **Pipeline health** | rtspsrc **reconnect churn** and **decoder/concealment errors** counted in the log tail (the symptom of packets lost upstream of decode) |
| **Capture CPU% & delivered bitrate** | capture process-tree CPU% (`/proc`), and the camera's delivered **Mbps** from raw-video segment size (no decode) — the back-pressure and camera-bandwidth signals |
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
| **Memory pressure (PSI)** | `full`/`some` stall % from `/proc/pressure/memory` — the actual pre-OOM signal (the kernel kills on reclaim failure, not a fixed free-MB line). Scale-independent, so it works on a 2 GB Pi and a 32 GB box with no per-host tuning. `MemAvailable`/`SwapFree` are still reported for context. |
| **CPU / I-O pressure** | busy% and **iowait%** (`/proc/stat` delta) + 1-min **load-per-core** (`/proc/loadavg`) — published as context for drop attribution (not alerted: heavy processing legitimately spikes them) |
| **Disk failure** | kernel-log scan for I/O errors / mmc(blk) errors / EXT4-fs errors / **read-only remount** — the medium-agnostic "disk failing" canary (worn SD cards included), which a slow-but-healthy card won't trip |
| **NIC errors / IP reassembly** | RX **hardware-error** growth (errs+fifo+frame, *not* `rx_dropped` — that's benign discarded multicast) from `/proc/net/dev`, **scoped to the camera-facing interface(s)** (resolved per camera IP via `/proc/net/route`, so an internet/wifi NIC on a dedicated-cam-subnet box is ignored; a shared single-NIC host resolves to that one NIC), plus `Ip.ReasmFails` from `/proc/net/snmp` (UDP only). `rx_dropped` and the watched `nic_cam_interfaces` are reported for context. |
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
| `platepar_mismatch` | error | `.config` resolution ≠ platepar `X_res`/`Y_res` — RMS discards the platepar, so the night gets **no astrometric calibration** (silent data killer) | — |
| `config_fov_mismatch` | degraded | the real FOV (platepar `fov_h`) is outside `[0.75×, 1.5×] config.fov_w` — astrometry.net's solve range, so a fresh auto-calibration would fail (latent; existing platepar still works) | — |
| `timelapse_missing` | degraded | a completed frame session's ffmpeg failed — its `_frametimes.json` exists but no `_frames_timelapse.mp4` | `timelapse_grace_s` (1h) |
| `timelapse_overdue` | degraded | saving frames but no `_frames_timelapse.mp4` produced in ages (generation not running at all; latitude-independent) | `timelapse_max_age_s` (30h) |
| `log_fatal` | error | `Traceback`/`ImportError`/`cannot open shared object`/segfault in the log | — |
| `log_warning` | degraded | actionable `WARNING`-level lines in the log tail (benign ones filtered — see below) | `log_warning_warn` (1 = any) |
| `watchdog` | degraded | RMS `WATCHDOG: died/stale/Restarting` event | — |
| `disk_low` | degraded / error | data partition free space low / critical | `disk_free_warn_gb` (20) / `disk_free_error_gb` (5) |
| `upload_backlog` | degraded | upload queue length over threshold (normally drains to 0 each morning; ~4/night when stuck) | `upload_queue_warn` (10) |
| `clock_unsynced` | degraded | last summary reported clock not synchronized | — |
| `clock_uncertainty` | degraded | last summary clock error over threshold | `clock_error_warn_ms` (100) |
| `dropped_frames` | degraded | dropped frames in the last 10 min — the alert text names the attributed **cause** (see below) | `dropped_frames_warn` (10) |
| `oom` | error (python victim) / degraded | host OOM-killer fired (kernel log) | — |
| `mem_pressure` | degraded / error | host memory **pressure** (PSI) — `full avg10` spiking / sustained `full avg60` high (pre-OOM, scale-independent) | `mem_psi_full_avg10_warn` (10) / `mem_psi_full_avg60_error` (10) |
| `udp_rcvbuf_errors` | degraded | host UDP RcvbufErrors growth rate (only when a station uses `protocol: udp`) | `udp_rcvbuf_errors_per_min_warn` (0 = any increase) |
| `nic_errors` | degraded | camera-facing NIC RX **hardware**-error growth (wire/cable/duplex/port; excludes benign `rx_dropped`) | `nic_rx_errors_per_min_warn` (0 = any increase) |
| `disk_errors` | degraded / error | kernel disk I/O errors / a **read-only remount** (a failing disk — incl. worn SD cards). Medium-agnostic; does NOT fire on a merely-slow card the way an iowait threshold would | — |

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

**Benign warnings:** `log_warning` ignores high-volume, non-actionable RMS
warnings by default — ExtractStars `Too many candidate stars`, numpy/scipy
`*Warning:` (covariance / empty slice / invalid divide), the observation-summary
lock race, `alignPlatepar: Fit did not converge` (self-recovers), and `Dropped
frames timestamp queue exceeded safety limit` (RMS memory-cap housekeeping —
already covered by the `dropped_frames` check), and `Fewer than N images found,
cannot create timelapse` (a tiny day/night-transition frame session), and `too
many sporadics per hour` (a Flux meteor-rate QC skip — science pipeline, not
station health). Add your own patterns with `log_warning_ignore` (regex).
Genuinely actionable warnings
(camera-switch / upload / ffmpeg / reboot failures, …) still alert.

> `Too many candidate stars` is in the default ignore list. It fires almost
> entirely on sky washout (moon / cloud / light dome / daytime), which isn't
> actionable, and the overflowing frames are *skipped* (logging zero stars) — so
> the logs can't reliably tell a too-low `max_star_candidates` from washout.
> Rather than alert misleadingly, it's simply suppressed.

## Dropped-frame attribution

When a station drops frames, "dropped frames" alone doesn't say *where*. So the
monitor attributes each drop to a **cause by elimination** — the same walk you'd
do by hand — using cheap signals collected every cycle, and adds three fields to
the station record: **`drop_cause`**, **`drop_confidence`**, **`drop_detail`**.
The `dropped_frames` alert text carries the cause, e.g.
*"Dropped 2214 frames in last 10 min — likely camera/link bandwidth (12 decoder
errors; 9 reconnects; 8.1 Mbps; host clean)"*.

The elimination order (cheapest/strongest first):

| `drop_cause` | Decided by | Signals |
|---|---|---|
| `cpu/io back-pressure` | the consumer fell behind | appsink fill **spiked** (recent max, since it recovers by the drop line) — `buffer_fill_max_recent`; host CPU%/iowait% shown as context, not as the trigger |
| `network: kernel UDP buffer` | socket overflow (raise `rmem_max`) | `udp_rcvbuf_errors_per_min` climbing |
| `network: NIC/wire` | the link itself shedding packets | `nic_rx_errors_per_min` climbing |
| `network: IP fragmentation` | fragments lost on reassembly | `ip_reasm_fails_per_min` climbing |
| `network: link packet loss` | sustained loss to the camera | probed `ping` loss% |
| `camera/link bandwidth` | damaged input, **host clean** | decoder/concealment errors + reconnects, with delivered Mbps / probed keyframe peak |
| `uncertain` | real drops, nothing positive yet | → triggers a probe to confirm |

All of the above is `/proc`- and `stat`-cheap, so it runs every cycle even on a
Pi. The two **heavy** confirmations — `ffprobe` of a recent segment for the
**keyframe peak**, and a **ping** of the camera — are not run in the loop. They
escalate **adaptively**: only when a station is dropping frames the host signals
*can't* explain (the `camera/link bandwidth` / `uncertain` rows), and then at
most once per `probe_min_interval_s` (10 min), **backing off** by doubling up to
`probe_max_interval_s` (1 h) while the camera stays bad — so a persistently-bad
stream is confirmed once, not re-hammered. Set `enable_adaptive_probe: false` to
disable in-loop probing entirely.

> Why this split: a 270 KB keyframe is ~20 UDP packets in <1 ms — a line-rate
> microburst a marginal switch port/cable drops *before* the host, invisible to
> `RcvbufErrors`, NIC counters, and `ping` (paced ICMP isn't a burst). Decoder
> corruption with every host counter clean is its fingerprint; the delivered
> bitrate / keyframe peak quantify it. Lowering the camera bitrate shrinks the
> burst below what the link drops — a workaround — while the real fix is the port/cable.

**On demand:** force the full probe (ignoring the threshold and backoff) and
print the attribution for one or all stations:

```bash
python -m cc_mqtt_monitor --diagnose CAWEC4   # one station
python -m cc_mqtt_monitor --diagnose          # all stations
```

`ffprobe` (from `ffmpeg`) and `ping` (`iputils-ping`) are used only by the
probes; if absent, the probe fields are simply null with a note — every cheap
signal and the loop keep working.

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
  "rms_branch": "master",
  "rms_up_to_date": true,
  "rms_behind_days": 0.0,
  "host": "us005-host",
  "timestamp": "2026-06-15T17:40:00Z"
}
```

`group` is the human-readable label (RMS `camera_group_name`, or the installer's
override); **`group_slug`** is the canonical subscription handle — spaces/
punctuation collapsed to `-` so it is valid as an ntfy topic / Telegram tag.

### What to subscribe to (handles)

The bridge fans each alert out to several **handles** — and a handle is the same
string on both channels: it's the ntfy topic name *and* the Telegram token, so it
means the same thing whichever app you use (see "How to receive them" below).
Pick your granularity:

| Handle | Covers |
|---|---|
| `<group_slug>` | your whole group (e.g. `Phoenix-1`) |
| `<stationID>` | one camera (e.g. `US005A`) |
| `<prefix>` | **a whole network** — any leading prefix of a station ID **of 3+ characters** (`USC`, `CAC`, `USL`, `USV`, …) |

The prefix axis is the important one for network coordinators: subscribe to
`USC` (or `CAC`, …) **once**, and every current *and future* station whose
ID starts with that prefix is covered automatically — no change is needed
when a new station is deployed, because the bridge publishes each station's
alerts to all of its ID prefixes (from 3 chars up to the full ID) as soon as its
monitor comes online. Prefixes shorter than 3 chars (`U`, `US`) are not
published — they'd carry every station's traffic. The network codes
(`USC`, `CAC`, `USL`, `USV`, …) are all 3 chars, so the floor covers each.

The host record carries `groups` + `group_slugs` + `station_ids`, so host-level
(OOM/memory) alerts fan out to the same group and prefix handles.

### How to receive them (Telegram or ntfy)

Pick a **token** from the table above — your `group_slug`, a single `stationID`,
or a 3+ char network prefix — then subscribe on either platform:

**Telegram — works on every platform (iOS, Android, desktop). Recommended,
especially on iPhone/iPad.** Open a chat with the bridge bot
(**[`@contrailcast_rms_bot`](https://t.me/contrailcast_rms_bot)**) and send:

```
/subscribe <token>      # e.g.  /subscribe Phoenix-1   or   /subscribe USC
/list                   # show your subscriptions
/unsubscribe <token>
```

A prefix token like `USC` auto-covers every current *and future* station with
that prefix — subscribe once.

**ntfy — recommended on Android / desktop / web; NOT recommended on iOS.**
Install the ntfy app, point it at the bridge's ntfy server
(**`https://ntfy.contrailcast.com`**), and add the handle **`<handle>`** as a
topic (e.g. `Phoenix-1`, `USC`) — or open `https://ntfy.contrailcast.com/<handle>`
in a browser. The installer prints your host's exact handles at the end of a
deploy.

> **Don't use ntfy on iOS — use Telegram.** Apple only allows background push
> via its APNs service, and a self-hosted ntfy server can't reach APNs directly:
> iOS pushes are relayed through the public `ntfy.sh` (`upstream-base-url`), which
> is **rate-limited** (a shared per-server bucket). So on iPhone/iPad ntfy alerts
> can be throttled, delayed, or dropped. Telegram has no such limit — use it on iOS.

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

## Uninstall

```bash
# From inside the checkout (or anywhere — it finds the install):
./scripts/uninstall_station.sh
```

This is the inverse of the installer and is safe to re-run. It:

1. stops, disables, and removes the systemd service **and** the auto-update timer;
2. clears this host's **retained broker records** (status + host + every station)
   so nothing lingers on the dashboard after removal.

By default it **leaves the checkout and `config.yaml` in place**, so a later
re-install keeps your settings. To remove those too (uninstall the package and
delete the folder):

```bash
CC_PURGE=1 ./scripts/uninstall_station.sh
```

> The systemd steps need `sudo` (same as install). If you don't have it, the
> manual equivalent is `sudo systemctl disable --now cc-rms-monitor.service
> cc-rms-monitor-update.timer` then remove `/etc/systemd/system/cc-rms-monitor*`.
> To clear the broker by hand: `python -m cc_mqtt_monitor --unpublish`.

## Usage

```bash
# Local diagnostics, no broker needed — prints each station's health as JSON:
python -m cc_mqtt_monitor --status

# Force the deep dropped-frame probe + print the attribution (one or all stations):
python -m cc_mqtt_monitor --diagnose CAWEC4
python -m cc_mqtt_monitor --diagnose

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
subscriptions (`<group_slug>` and the network `<prefix>`). It uses a
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
- Optional: `ffmpeg` (for `ffprobe`) and `iputils-ping` — used **only** by the
  deep dropped-frame probe (`--diagnose` / adaptive escalation). Absent → those
  probe fields are null; every cheap signal and the loop are unaffected.
