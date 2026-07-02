"""Unit checks for the mtime hot-reload (config.reload_config).

An edit to config.yaml is picked up on the next cycle without a restart, but only
for content fields (thresholds, group, disabled_checks, ...). Connection/identity
fields (broker, host_name, topic_prefix) stay pinned to the startup values -- a
change to those needs a restart and is reported via `pinned_changed`.

Runs under pytest, or standalone: `python tests/test_config_reload.py`.
"""

import os
import sys
import tempfile
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor.config import load_config, reload_config     # noqa: E402


def _cfg_file():
    path = os.path.join(tempfile.mkdtemp(), "config.yaml")
    def write(text):
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(text))
    return path, write


def test_content_fields_reload_without_restart():
    path, write = _cfg_file()
    write("host_name: SiteA\ngroup: G1\nthresholds:\n  upload_queue_warn: 10\n")
    cur = load_config(path)
    write("host_name: SiteA\ngroup: G2\ndisabled_checks: [dropped_frames]\n"
          "thresholds:\n  upload_queue_warn: 4\n")
    new, pinned = reload_config(cur, path)
    assert pinned == []                              # nothing pinned changed
    assert new.group == "G2"
    assert new.thresholds.upload_queue_warn == 4
    assert new.disabled_checks == ["dropped_frames"]


def test_identity_fields_are_pinned_and_reported():
    path, write = _cfg_file()
    write("host_name: SiteA\ntopic_prefix: stations\n")
    cur = load_config(path)
    write("host_name: SiteB\ntopic_prefix: other\n")
    new, pinned = reload_config(cur, path)
    assert set(pinned) == {"host_name", "topic_prefix"}
    assert new.host_name == "SiteA"                  # pinned to startup
    assert new.topic_prefix == "stations"


def test_broker_change_is_pinned():
    path, write = _cfg_file()
    write("broker:\n  host: a.example.com\n  port: 1883\n")
    cur = load_config(path)
    write("broker:\n  host: b.example.com\n  port: 8883\n")
    new, pinned = reload_config(cur, path)
    assert "broker" in pinned
    assert new.broker.host == "a.example.com" and new.broker.port == 1883


def test_invalid_file_keeps_current_config():
    path, write = _cfg_file()
    write("host_name: SiteA\ngroup: G1\n")
    cur = load_config(path)
    write("host_name: [unterminated\n  : : :\n")     # not valid YAML
    new, pinned = reload_config(cur, path)
    assert new is cur and pinned is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d config-reload tests passed." % len(fns))
