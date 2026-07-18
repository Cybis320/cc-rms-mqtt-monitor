"""Transient UploadManager warnings must NOT alert; persistent config errors must.

RMS's UploadManager logs a WARNING on every transient retry / DNS / network blip and
self-heals; a GENUINELY persistent upload failure backs the queue up so upload_backlog
fires with the real count. So the transient warnings are muted by default (they were
~90/117 upload alerts in a sample week). The persistent CONFIG errors (bad/missing key)
stay as specific, actionable alerts.

Runs under pytest, or standalone: `python tests/test_upload_warnings.py`.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                       # noqa: E402

IGNORE = collect._compile_warning_ignore(None)   # built-in defaults only

# real log lines (redacted), as they appear after the "-WARNING-UploadManager-line:N - " tag
TRANSIENT = [
    "UploadManager-line:120 - Uploading failed! Retry 5 of 5",
    "UploadManager-line:88 - SSH connection failed during agent fallback: [Errno -3] Temporary failure in name resolution",
    "UploadManager-line:88 - SSH connection failed during agent fallback: [Errno None] Unable to connect to port 22 on 1.2.3.4",
    "UploadManager-line:88 - SSH connection failed during agent fallback: [Errno 101] Network is unreachable",
]
PERSISTENT = [
    "UploadManager-line:64 - Agent authentication failed. No valid authorized keys found.",
    "UploadManager-line:70 - SSH error with provided key: encountered RSA key, expected OPENSSH key",
    "UploadManager-line:75 - IO error with key file: [Errno None] Unable to connect to port 22 on 1.2.3.4",
]


def test_transient_upload_warnings_ignored():
    for line in TRANSIENT:
        assert IGNORE.search(line), "should be muted: %s" % line


def test_persistent_upload_config_errors_still_alert():
    for line in PERSISTENT:
        assert not IGNORE.search(line), "should still alert: %s" % line


def test_non_upload_warning_unaffected():
    # a normal RMS warning is unaffected by these upload patterns
    assert not IGNORE.search("SomeModule - unexpected thing happened")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); print("ok", name)
