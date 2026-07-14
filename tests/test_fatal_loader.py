"""Unit checks for loader-diagnostic demotion in the fatal scan.

RMS pipes GStreamer/GLib output through its own logger at INFO level, and GStreamer
probes for OPTIONAL plugins at startup. A probe miss logs "cannot open shared object
file" verbatim while nothing is actually broken -- the plugin is just skipped. Those
must not read as a fatal error. A loader failure that really kills RMS carries a
Traceback/ImportError, or lands in the journal (no RMS level field) -- both still fire.

Runs under pytest, or standalone: `python tests/test_fatal_loader.py`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_mqtt_monitor import collect                      # noqa: E402


def _fatal(line):
    return collect._fatal_match(line) is not None


# The real false positive (2026-07-14): GStreamer's CUDA plugin probing for libnvrtc
# on a box with no NVIDIA stack. INFO level, entirely cosmetic.
NVRTC = ("2026/07/14 05:10:01-INFO-Logger-line:264 - cudanvrtc "
         "../gst-libs/gst/cuda/gstcudanvrtc.cpp:152:gst_cuda_nvrtc_load_library_once: "
         "Could not open nvrtc library libnvrtc.so: cannot open shared object file: "
         "No such file or directory")


def test_nvrtc_probe_is_not_fatal():
    assert not _fatal(NVRTC)


def test_info_level_undefined_symbol_is_not_fatal():
    assert not _fatal("2026/07/14 05:10:01-INFO-Logger-line:264 - plugin: undefined symbol: foo")


def test_debug_level_loader_miss_is_not_fatal():
    assert not _fatal("2026/07/14 05:10:01-DEBUG-Logger-line:264 - cannot open shared object file")


def test_error_level_loader_failure_is_still_fatal():
    # A real loader failure at ERROR level must still fire.
    assert _fatal("2026/07/14 05:10:01-ERROR-Logger-line:264 - "
                  "libopencv.so: cannot open shared object file: No such file or directory")


def test_journal_loader_failure_is_still_fatal():
    # Journal lines have no RMS level field -> never demoted (the import-crash case).
    assert _fatal("python3[1234]: ImportError: libcblas.so.3: cannot open shared object file")
    assert _fatal("python3[1234]: libfoo.so: cannot open shared object file")


def test_info_line_with_a_real_fatal_still_counts():
    # Demotion must not swallow a genuine fatal that happens to sit on an INFO line:
    # the scan continues past the loader pattern.
    assert _fatal("2026/07/14 05:10:01-INFO-Logger-line:264 - ImportError: libfoo.so: "
                  "cannot open shared object file")
    assert _fatal("2026/07/14 05:10:01-INFO-Logger-line:264 - Segmentation fault")


def test_ordinary_fatals_unaffected():
    assert _fatal("Traceback (most recent call last):")
    assert _fatal("ModuleNotFoundError: No module named 'cv2'")
    assert _fatal("2026/07/14 05:10:01-ERROR-x-line:1 - Segmentation fault (core dumped)")


def test_benign_info_line_is_not_fatal():
    assert not _fatal("2026/07/14 05:10:01-INFO-BufferedCapture-line:2323 - Buffer fill: 5.0%")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print("ok:", fn.__name__)
    print("\nAll %d loader-demotion tests passed." % len(fns))
