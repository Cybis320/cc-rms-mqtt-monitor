"""Unit checks for scoping kernel disk errors to the OS / RMS_data devices.

An I/O error on an unrelated drive (a USB stick, a scratch disk) is not a station
problem and must not raise disk_errors. Only the device backing the OS root or a
station's RMS data dir counts. A line naming no device at all fails OPEN (counted),
so a genuine failure is never silently dropped.

Runs under pytest, or standalone: `python tests/test_disk_scope.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import oslevel                      # noqa: E402


def _scan(lines, relevant):
    """Run the scan with a pinned 'relevant disks' set (no /proc/mounts needed)."""
    real = oslevel._relevant_disks
    oslevel._relevant_disks = lambda _paths: set(relevant)
    try:
        return oslevel.scan_disk_errors(lines=lines, data_paths=["/dummy"])
    finally:
        oslevel._relevant_disks = real


# The real false positive (2026-07-14): sdb1 is an unrelated drive.
USB = ("2026-07-14T00:19:45+00:00 EliteCamSFF12 kernel: Buffer I/O error on dev "
       "sdb1, logical block 485, lost async page write")
ROOT = ("2026-07-14T00:19:45+00:00 EliteCamSFF12 kernel: Buffer I/O error on dev "
        "sda2, logical block 12, lost async page write")


def test_unrelated_device_is_not_counted():
    r = _scan([USB], relevant={"sda"})
    assert r["disk_error_count"] == 0
    assert r["disk_error_other_dev_count"] == 1     # visible, not alerted
    assert r["last_disk_error"] is None


def test_os_disk_error_still_counted():
    r = _scan([ROOT], relevant={"sda"})
    assert r["disk_error_count"] == 1
    assert "sda2" in r["last_disk_error"]


def test_partition_maps_to_its_whole_disk():
    # The data dir resolves to mmcblk0; the error is reported on mmcblk0p2.
    line = "kernel: EXT4-fs error (device mmcblk0p2): ext4_find_entry: I/O error"
    assert _scan([line], relevant={"mmcblk0"})["disk_error_count"] == 1


def test_readonly_remount_on_other_device_is_ignored():
    line = "kernel: EXT4-fs (sdb1): Remounting filesystem read-only"
    r = _scan([line], relevant={"sda"})
    assert r["disk_error_count"] == 0
    assert r["disk_fs_readonly"] is False


def test_readonly_remount_on_os_disk_still_severe():
    line = "kernel: EXT4-fs (sda2): Remounting filesystem read-only"
    r = _scan([line], relevant={"sda"})
    assert r["disk_error_count"] == 1
    assert r["disk_fs_readonly"] is True


def test_line_naming_no_device_fails_open():
    # Can't attribute it -> count it rather than silently drop a real failure.
    line = "kernel: Remounting filesystem read-only"
    r = _scan([line], relevant={"sda"})
    assert r["disk_error_count"] == 1
    assert r["disk_fs_readonly"] is True


def test_hostname_is_not_mistaken_for_a_device():
    # "EliteCamSFF12" must not parse as a device token (word-anchored regex).
    assert oslevel._DEV_TOKEN_RE.findall(USB) == ["sdb1"]


def test_unresolvable_paths_count_everything():
    # If the relevant set comes back empty, scope nothing out (fail open).
    r = _scan([USB], relevant=set())
    assert r["disk_error_count"] == 1


def test_base_disk_mapping():
    assert oslevel._base_disk("sdb1") == "sdb"
    assert oslevel._base_disk("mmcblk0p2") == "mmcblk0"
    assert oslevel._base_disk("nvme0n1p1") == "nvme0n1"
    assert oslevel._base_disk("sda") == "sda"


def test_real_mount_lookup_resolves_root():
    # Sanity: the live host's "/" resolves to some device (not None).
    assert oslevel._mount_device("/") is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d disk-scope tests passed." % len(fns))
