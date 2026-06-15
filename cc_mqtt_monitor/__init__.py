"""CC RMS MQTT health monitor.

A standalone, host-level agent that discovers every RMS station configured on a
machine, gathers health signals for each, and publishes them to an MQTT broker
as both a plain-JSON state blob (for custom dashboards) and Home Assistant MQTT
Discovery entities.
"""

__version__ = "0.1.0"
