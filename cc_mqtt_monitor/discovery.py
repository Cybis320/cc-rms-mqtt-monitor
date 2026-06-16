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
    # Operator-defined grouping straight from the RMS .config (camera cluster /
    # location). None when unset ("none"). This is the primary subscription group.
    camera_group_name: str = None
    # Capture mode / location, used to know what output to expect when.
    continuous_capture: bool = False
    switch_camera_modes: bool = False
    save_frames: bool = True
    latitude: float = 0.0
    longitude: float = 0.0

    @property
    def captured_path(self):
        return os.path.join(self.data_dir, self.captured_dir)

    @property
    def frames_path(self):
        return os.path.join(self.data_dir, self.frame_dir)

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


def _station_from_config(config_path):
    cfg = _read_config(config_path)
    station_id = cfg.get("stationID") or os.path.basename(os.path.dirname(config_path))
    data_dir = os.path.expanduser(cfg.get("data_dir", _DEFAULTS["data_dir"]))
    # RMS accepts latitude/longitude or the short lat/lon keys.
    latitude = _as_float(cfg.get("latitude", cfg.get("lat")))
    longitude = _as_float(cfg.get("longitude", cfg.get("lon")))

    # camera_group_name: treat "none"/blank as unset.
    group = (cfg.get("camera_group_name") or "").strip()
    camera_group_name = group if group and group.lower() != "none" else None

    return Station(
        station_id=station_id,
        config_path=os.path.abspath(config_path),
        data_dir=data_dir,
        captured_dir=cfg.get("captured_dir", _DEFAULTS["captured_dir"]),
        archived_dir=cfg.get("archived_dir", _DEFAULTS["archived_dir"]),
        log_dir=cfg.get("log_dir", _DEFAULTS["log_dir"]),
        upload_queue_file=cfg.get("upload_queue_file", _DEFAULTS["upload_queue_file"]),
        frame_dir=cfg.get("frame_dir", "FramesFiles"),
        camera_group_name=camera_group_name,
        continuous_capture=_as_bool(cfg.get("continuous_capture")),
        switch_camera_modes=_as_bool(cfg.get("switch_camera_modes")),
        save_frames=_as_bool(cfg.get("save_frames"), default=True),
        latitude=latitude,
        longitude=longitude,
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
