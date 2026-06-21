"""Unit checks for cc_mqtt_monitor.sanitize.redact.

Runs under pytest, or standalone: `python tests/test_sanitize.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor.sanitize import redact   # noqa: E402


def test_device_url_password_and_creds_masked():
    line = ('2026/06/21 02:52:02-WARNING-BufferedCapture - location='
            '"rtsp://192.168.42.104:554/user=admin&password=hunter2&channel=1"')
    out = redact(line)
    assert "hunter2" not in out          # the camera password must be gone
    assert "admin" not in out            # the username too
    assert "192.168.42.104" not in out   # and the LAN IP
    assert "password=***" in out
    assert "<ip>" in out
    assert "channel=1" in out            # non-secret context preserved
    assert "02:52:02" in out             # timestamp not mangled as an IPv6


def test_ssh_warning_ip_masked_message_readable():
    line = ("2026/06/21 02:52:02-WARNING-UploadManager-line:238 - SSH connection "
            "failed during agent fallback: [Errno None] Unable to connect to "
            "port 22 on 129.100.18.139")
    out = redact(line)
    assert "129.100.18.139" not in out
    assert "<ip>" in out
    assert "port 22" in out                       # port is not an IP -> kept
    assert "SSH connection failed" in out         # still triageable


def test_token_and_secret_masked():
    assert redact("api_key=AB12cd34") == "api_key=***"
    assert redact("token: ZZZsecretZZZ") == "token: ***"
    assert "s3cr3t" not in redact("AWS_SECRET_KEY=s3cr3t blah")


def test_url_userinfo_masked():
    assert redact("sftp://luc:pw@host/path") == "sftp://***@host/path"


def test_home_path_username_masked():
    assert "/home/ops" not in redact("FileNotFound: /home/ops/RMS_data/x.fits")
    assert "~/RMS_data/x.fits" in redact("FileNotFound: /home/ops/RMS_data/x.fits")


def test_empty_password_not_over_redacted():
    # RMS frequently uses an empty password=&; nothing secret, stay readable.
    out = redact("user=admin&password=&channel=1")
    assert "password=" in out and "password=***" not in out
    assert "user=***" in out


def test_none_and_plain_passthrough():
    assert redact(None) is None
    assert redact("") == ""
    assert redact("Block interval: mean 1.0, max 1.2") == \
        "Block interval: mean 1.0, max 1.2"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d sanitize tests passed." % len(fns))
