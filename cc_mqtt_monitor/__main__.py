"""Command-line entry point.

    cc-rms-monitor                 # run the publish loop (default)
    cc-rms-monitor --once          # one collect+publish cycle, then exit
    cc-rms-monitor --status        # print station health locally, no MQTT
    cc-rms-monitor --viewer        # subscribe and print a live status table
    cc-rms-monitor --config FILE   # use a YAML config (else built-in defaults)
"""

import sys
import json
import logging
import argparse

from .config import load_config
from . import monitor


def _setup_logging(verbose):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="cc-rms-monitor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", help="path to YAML config file")
    parser.add_argument("--once", action="store_true",
                        help="run a single collect+publish cycle and exit")
    parser.add_argument("--status", action="store_true",
                        help="print station health as JSON locally (no MQTT)")
    parser.add_argument("--diagnose", nargs="?", const="", metavar="STATION",
                        help="force the deep dropped-frame probe (ffprobe keyframe "
                             "peak + camera ping) on STATION (or all stations if "
                             "omitted), print the attribution, then exit -- no MQTT")
    parser.add_argument("--viewer", action="store_true",
                        help="subscribe to the broker and show a live status table")
    parser.add_argument("--test", action="store_true",
                        help="publish one test alert to exercise the broker->ntfy/Telegram chain, then exit")
    parser.add_argument("--test-udp", nargs="?", const=999.0, type=float, metavar="RATE",
                        help="publish one transient host-level UDP RcvbufErrors test alert "
                             "(optional simulated RATE per minute, default 999) so the bridge "
                             "can see the host alert path, then exit")
    parser.add_argument("--unpublish", action="store_true",
                        help="clear this host's retained broker records (status, host, "
                             "and every station) and exit -- used by the uninstaller so "
                             "nothing lingers on the dashboard after removal")
    parser.add_argument("--interval", type=int,
                        help="override the polling interval in seconds")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)
    if args.interval:
        config.interval_seconds = args.interval

    # --status: pure local diagnostics, no broker connection needed. The host
    # record is gathered first and passed into station evaluation so dropped-
    # frame attribution can use the host-wide CPU/NIC/UDP signals.
    if args.status:
        host = monitor.gather_host(config)
        output = {
            "host": host,
            "stations": monitor.gather(config, host_metrics=host),
        }
        print(json.dumps(output, indent=2, default=str))
        return 0

    # --diagnose: force the heavy probe and print the dropped-frame attribution.
    if args.diagnose is not None:
        host, reports = monitor.run_diagnose(config, args.diagnose or None)
        if not reports:
            print("No matching station%s."
                  % (" '%s'" % args.diagnose if args.diagnose else ""))
            return 1
        for st in reports:
            print("=== %s ===" % st["station_id"])
            cause = st.get("drop_cause")
            if cause:
                print("  dropped (10min): %s" % st.get("dropped_frames_10min"))
                print("  cause: %s (%s) -- %s"
                      % (cause, st.get("drop_confidence"), st.get("drop_detail")))
            else:
                print("  no dropped frames to attribute (10min: %s)"
                      % st.get("dropped_frames_10min"))
            for k in ("buffer_fill_pct", "capture_cpu_pct", "decoder_errors",
                      "pipeline_reconnects", "stream_mbps", "probe_keyframe_peak_kb",
                      "probe_stream_mbps", "probe_ping_loss_pct", "probe_ping_rtt_max_ms",
                      "probe_keyframe_note", "probe_ping_note"):
                if st.get(k) is not None:
                    print("    %-22s %s" % (k, st[k]))
        if host:
            print("=== host ===")
            for k in ("cpu_busy_pct", "cpu_iowait_pct", "load_per_core",
                      "nic_rx_errors_per_min", "udp_rcvbuf_errors_per_min",
                      "ip_reasm_fails_per_min"):
                if host.get(k) is not None:
                    print("    %-22s %s" % (k, host[k]))
        return 0

    if args.viewer:
        from .viewer import Viewer
        Viewer(config).run()
        return 0

    # --test: publish one transient test alert (no host-status side effects).
    if args.test:
        from .publisher import Publisher
        state = monitor.make_test_state(config)
        publisher = Publisher(config, announce=False)
        publisher.connect()
        try:
            publisher.publish_test(state)
        finally:
            publisher.disconnect()
        sid, gs = state["station_id"], state.get("group_slug")
        logging.getLogger("cc_mqtt_monitor").info(
            "Test alert published to %s/%s/health (non-retained).",
            config.topic_prefix, sid)
        print("Test alert sent. Subscribers should receive it on:")
        if gs:
            print("    cc-%s        (your group)" % gs)
        print("    cc-%s   (this station) and any prefix, e.g. cc-%s" % (sid, sid[:3]))
        return 0

    # --test-udp: publish one transient host-level UDP RcvbufErrors test alert.
    if args.test_udp is not None:
        from .publisher import Publisher
        state = monitor.make_udp_test_state(config, args.test_udp)
        publisher = Publisher(config, announce=False)
        publisher.connect()
        try:
            publisher.publish_test_host(state)
        finally:
            publisher.disconnect()
        print("UDP test host alert sent (non-retained) to %s/%s/health:"
              % (config.topic_prefix, config.host_name))
        print(json.dumps(state, indent=2, default=str))
        print("\nBridge should route it to:", ", ".join(
            "cc-%s" % gs for gs in state.get("group_slugs") if gs) or "(no group_slug)")
        return 0

    # --unpublish: wipe this host's retained records so nothing lingers after
    # the agent is removed (used by the uninstaller). Clean disconnect => no
    # "offline" Last-Will is left behind either.
    if args.unpublish:
        from .publisher import Publisher
        from .discovery import discover_stations
        sids = [s.station_id for s in discover_stations(config.stations_dir, config.rms_dir)]
        publisher = Publisher(config, announce=False)
        publisher.connect()
        publisher.go_silent(sids)   # clears status + host + each station, then disconnects
        print("Cleared retained broker records for host '%s'%s."
              % (config.host_name, (" and stations %s" % sids) if sids else ""))
        return 0

    # Publishing paths need paho + a broker.
    from .publisher import Publisher
    publisher = Publisher(config)
    if args.once:
        publisher.connect()
        try:
            monitor.run_once(config, publisher)
        finally:
            publisher.disconnect()
    else:
        monitor.run_loop(config, publisher, args.config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
