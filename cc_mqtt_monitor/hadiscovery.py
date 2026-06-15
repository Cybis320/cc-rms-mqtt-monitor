"""Build Home Assistant MQTT Discovery configs.

Each station becomes one HA "device"; each metric below becomes an entity whose
state is read from the station's plain-JSON state topic via a value_template.
The same state topic feeds custom dashboards, so the two consumers never
diverge. Entities reference the host availability (LWT) topic, so they show as
"unavailable" the moment the monitor agent dies.
"""

# (component, key, friendly_name, options)
#   component: "sensor" or "binary_sensor"
#   key:       JSON field in the state payload
_ENTITIES = [
    ("sensor", "status", "Status", {"icon": "mdi:heart-pulse"}),
    ("binary_sensor", "capture_alive", "Capture running",
     {"device_class": "running"}),
    ("sensor", "newest_fits_age_s", "Newest FITS age",
     {"unit_of_measurement": "s", "icon": "mdi:timer-outline"}),
    ("sensor", "fits_count", "FITS in session", {"icon": "mdi:image-multiple"}),
    ("sensor", "fatal_error_count", "Fatal log errors",
     {"icon": "mdi:alert-circle"}),
    ("sensor", "last_error", "Last error", {"icon": "mdi:bug"}),
    ("sensor", "disk_free_gb", "Disk free",
     {"unit_of_measurement": "GB", "icon": "mdi:harddisk"}),
    ("sensor", "upload_queue_len", "Upload backlog",
     {"icon": "mdi:cloud-upload"}),
    ("sensor", "dropped_frames_10min", "Dropped frames (10 min)",
     {"icon": "mdi:filmstrip-off"}),
    ("sensor", "total_rss_mb", "Process memory",
     {"unit_of_measurement": "MB", "icon": "mdi:memory"}),
    ("sensor", "problems", "Problems", {"icon": "mdi:format-list-bulleted"}),
]


def _value_template(component, key):
    if component == "binary_sensor":
        return "{{ 'ON' if value_json.%s else 'OFF' }}" % key
    if key == "problems":
        # Render the list as a readable string (truncated to HA's 255-char limit).
        return "{{ (value_json.problems | join(', '))[:255] if value_json.problems else 'none' }}"
    return "{{ value_json.%s if value_json.%s is not none else 'unknown' }}" % (key, key)


def discovery_messages(station_id, state_topic, availability_topic, ha_prefix):
    """Yield (config_topic, payload_dict) for every entity of one station."""
    device = {
        "identifiers": ["rms_%s" % station_id],
        "name": "RMS %s" % station_id,
        "model": "RPi Meteor Station",
        "manufacturer": "CroatianMeteorNetwork",
    }
    for component, key, name, options in _ENTITIES:
        unique_id = "rms_%s_%s" % (station_id, key)
        config_topic = "%s/%s/%s/%s/config" % (ha_prefix, component, station_id, key)
        payload = {
            "name": name,
            "unique_id": unique_id,
            "object_id": unique_id,
            "state_topic": state_topic,
            "value_template": _value_template(component, key),
            "availability_topic": availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "device": device,
        }
        payload.update(options)
        yield config_topic, payload
