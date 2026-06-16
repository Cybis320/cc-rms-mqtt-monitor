"""CC RMS MQTT health monitor.

A standalone, host-level agent that discovers every RMS station configured on a
machine, gathers health signals for each, and publishes them to an MQTT broker
as retained plain-JSON state blobs (consumed by a broker-side ntfy/Telegram
alert bridge and any custom dashboards).
"""

__version__ = "0.1.0"
