"""Discover RMS stations configured on this host.

A station is any ``<stations_dir>/*/.config`` file. We parse the minimum needed
to locate its data: the station ID and the data directory, plus the standard
RMS subdirectory names (with RMS defaults as a fallback).
"""

import os
import glob
from dataclasses import dataclass

try:
    import configparser
except ImportError:  # pragma: no cover - Python 2 fallback, unlikely
    import ConfigParser as configparser


# RMS default subdirectory names, used when a station .config omits them.
_DEFAULTS = {
    "data_dir": "~/RMS_data",
    "captured_dir": "CapturedFiles",
    "archived_dir": "ArchivedFiles",
    "log_dir": "logs",
    "upload_queue_file": "FILES_TO_UPLOAD.inf",
}


@dataclass
class Station:
    station_id: str
    config_path: str
    data_dir: str
    captured_dir: str
    archived_dir: str
    log_dir: str
    upload_queue_file: str
    frame_dir: str = "FramesFiles"
    # Raw video segments (RMS `raw_video_save`): their on-disk size is the cheap
    # read on delivered camera bandwidth (bytes/segment-seconds = bitrate), with
    # no decode. Only present when raw_video_save is on.
    video_dir: str = "VideoFiles"
    raw_video_save: bool = False
    raw_video_duration: float = 30.0
    platepar_name: str = "platepar_cmn2010.cal"
    # Operator-defined grouping straight from the RMS .config (camera cluster /
    # location). None when unset ("none"). This is the primary subscription group.
    camera_group_name: str = None
    # Capture mode / location, used to know what output to expect when.
    continuous_capture: bool = False
    switch_camera_modes: bool = False
    save_frames: bool = True
    timelapse_generate_from_frames: bool = True
    # Operator consent to show this camera publicly (RMS "show on GMN weblog").
    # When false, the monitor must not publish this station to MQTT.
    weblog_enable: bool = True
    latitude: float = 0.0
    longitude: float = 0.0
    elevation: float = 0.0
    # Multi-camera switch stagger (one of RMS's programmed switch delays).
    capture_wait_seconds: float = 0.0
    # RTSP transport ("tcp"/"udp"). UDP can overflow the kernel receive buffer
    # (RcvbufErrors), which we monitor host-wide when any station uses it.
    protocol: str = "tcp"
    # Camera resolution from the .config. RMS DISCARDS the platepar entirely if it
    # was fit at a different resolution (no astrometry for that night) -- so a
    # mismatch vs the platepar X_res/Y_res is a silent, data-killing failure.
    config_width: int = 0
    config_height: int = 0
    # The camera's RTSP URL and the host/IP parsed from it (for the on-demand
    # network/keyframe probes). None when the device isn't an rtsp:// URL
    # (e.g. a v4l2 device index), in which case those probes are skipped.
    device_url: str = None
    camera_host: str = None

    @property
    def captured_path(self):
        return os.path.join(self.data_dir, self.captured_dir)

    @property
    def frames_path(self):
        return os.path.join(self.data_dir, self.frame_dir)

    @property
    def video_path(self):
        return os.path.join(self.data_dir, self.video_dir)

    @property
    def platepar_path(self):
        # The platepar lives in the station's config directory (next to .config).
        return os.path.join(os.path.dirname(self.config_path), self.platepar_name)

    @property
    def has_location(self):
        return not (self.latitude == 0.0 and self.longitude == 0.0)

    @property
    def archived_path(self):
        return os.path.join(self.data_dir, self.archived_dir)

    @property
    def log_path(self):
        return os.path.join(self.data_dir, self.log_dir)

    @property
    def upload_queue_path(self):
        return os.path.join(self.data_dir, self.upload_queue_file)


def _read_config(config_path):
    """Read an RMS .config (INI-like) leniently, returning the [Capture]/[System] keys."""
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # preserve case
    try:
        parser.read(config_path)
    except configparser.Error:
        return {}

    merged = {}
    for section in parser.sections():
        for key, value in parser.items(section):
            # Strip inline comments and whitespace the way RMS tolerates them.
            merged[key] = value.split(";")[0].strip()
    return merged


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _camera_host(device_url):
    """Host/IP from an RMS `device` value, or None if it isn't an rtsp:// URL.

    RMS device URLs look like
        rtsp://192.168.42.104:554/user=admin&password=&channel=1&stream=0.sdp
    so we take the netloc between '://' and the first '/', drop any 'user@'
    credentials and ':port', and return the bare host. A non-URL device (a v4l2
    index, a path) yields None, which makes the network/keyframe probes skip it.
    """
    if not device_url or "://" not in device_url:
        return None
    netloc = device_url.split("://", 1)[1].split("/", 1)[0]
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
    host = netloc.split(":", 1)[0].strip()
    return host or None


def _station_from_config(config_path):
    cfg = _read_config(config_path)
    station_id = cfg.get("stationID") or os.path.basename(os.path.dirname(config_path))
    data_dir = os.path.expanduser(cfg.get("data_dir", _DEFAULTS["data_dir"]))
    # RMS accepts latitude/longitude or the short lat/lon keys.
    latitude = _as_float(cfg.get("latitude", cfg.get("lat")))
    longitude = _as_float(cfg.get("longitude", cfg.get("lon")))
    elevation = _as_float(cfg.get("elevation", cfg.get("elev")))
    capture_wait_seconds = _as_float(cfg.get("capture_wait_seconds"))

    # camera_group_name: treat "none"/blank as unset.
    group = (cfg.get("camera_group_name") or "").strip()
    camera_group_name = group if group and group.lower() != "none" else None

    protocol = (cfg.get("protocol") or "tcp").strip().lower()
    device_url = (cfg.get("device") or "").strip() or None
    config_width = int(_as_float(cfg.get("width")))
    config_height = int(_as_float(cfg.get("height")))

    return Station(
        station_id=station_id,
        config_path=os.path.abspath(config_path),
        data_dir=data_dir,
        captured_dir=cfg.get("captured_dir", _DEFAULTS["captured_dir"]),
        archived_dir=cfg.get("archived_dir", _DEFAULTS["archived_dir"]),
        log_dir=cfg.get("log_dir", _DEFAULTS["log_dir"]),
        upload_queue_file=cfg.get("upload_queue_file", _DEFAULTS["upload_queue_file"]),
        frame_dir=cfg.get("frame_dir", "FramesFiles"),
        video_dir=cfg.get("video_dir", "VideoFiles"),
        raw_video_save=_as_bool(cfg.get("raw_video_save"), default=False),
        raw_video_duration=_as_float(cfg.get("raw_video_duration"), default=30.0),
        platepar_name=cfg.get("platepar_name", "platepar_cmn2010.cal"),
        camera_group_name=camera_group_name,
        continuous_capture=_as_bool(cfg.get("continuous_capture")),
        switch_camera_modes=_as_bool(cfg.get("switch_camera_modes")),
        save_frames=_as_bool(cfg.get("save_frames"), default=True),
        timelapse_generate_from_frames=_as_bool(
            cfg.get("timelapse_generate_from_frames"), default=True),
        weblog_enable=_as_bool(cfg.get("weblog_enable"), default=True),
        config_width=config_width,
        config_height=config_height,
        latitude=latitude,
        longitude=longitude,
        elevation=elevation,
        capture_wait_seconds=capture_wait_seconds,
        protocol=protocol,
        device_url=device_url,
        camera_host=_camera_host(device_url),
    )


def discover_stations(stations_dir, rms_dir=None):
    """Discover stations for either deployment scheme.

    Multicam: one station per ``<stations_dir>/*/.config``.
    Single-cam: a single station defined by ``<rms_dir>/.config``, used only as
    a fallback when no multicam stations are present (on a multicam box that
    same file is merely the template).
    """
    stations = []
    pattern = os.path.join(os.path.expanduser(stations_dir), "*", ".config")
    for config_path in sorted(glob.glob(pattern)):
        stations.append(_station_from_config(config_path))

    if not stations and rms_dir:
        single = os.path.join(os.path.expanduser(rms_dir), ".config")
        if os.path.isfile(single):
            stations.append(_station_from_config(single))

    return stations
