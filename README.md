# CC RMS MQTT Monitor

A standalone, host-level agent that monitors the health of every
[RMS](https://github.com/CroatianMeteorNetwork/RMS) meteor-camera station on a
machine and publishes it to an MQTT broker. State is published in two
complementary forms from the same data:

- **Plain JSON** — one retained blob per station for custom dashboards.
- **Home Assistant MQTT Discovery** — auto-created entities, no UI code.

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

## Health topics

```
contrailcast/rms/<host>/status            retained "online"/"offline" (Last Will)
contrailcast/rms/<host>/health            retained JSON host (OS) state blob
contrailcast/rms/<station>/health         retained JSON per-station state blob
homeassistant/<component>/<station>/<key>/config   retained HA discovery
```

The host status topic is the MQTT **Last Will** target, so a crashed agent or
offline host is detected without polling, and HA entities flip to "unavailable".

Example `health` payload:

```json
{
  "station_id": "US005A",
  "status": "error",
  "problems": ["Detection pipeline produced no output after 2100s of capture",
               "Fatal error in log (3x): ImportError: ... cannot open shared object file"],
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

## Install

```bash
./scripts/install.sh          # installs into ~/vRMS, creates config.yaml
```

Then edit `config.yaml` (at minimum the broker host) — see
`config.example.yaml` for every option.

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
```

Run as a service: see `systemd/cc-rms-monitor.service`.

## Design notes

- **Read-only / non-invasive.** Reads `/proc`, files, and logs; it never touches
  RMS processes or data, so it cannot perturb capture.
- **No RMS import.** It parses the `.config` and on-disk artifacts directly, so it
  runs independently of the RMS code version and venv state.
- **Defensive collectors.** A missing directory or down process yields empty
  metrics, never an exception — one broken station never stops the others.

## Requirements

- Python 3.7+
- `paho-mqtt`, `pyyaml` (installed by `scripts/install.sh`)
