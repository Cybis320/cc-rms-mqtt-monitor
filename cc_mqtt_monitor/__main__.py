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
    parser.add_argument("--viewer", action="store_true",
                        help="subscribe to the broker and show a live status table")
    parser.add_argument("--test", action="store_true",
                        help="publish one test alert to exercise the broker->ntfy/Telegram chain, then exit")
    parser.add_argument("--interval", type=int,
                        help="override the polling interval in seconds")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    config = load_config(args.config)
    if args.interval:
        config.interval_seconds = args.interval

    # --status: pure local diagnostics, no broker connection needed.
    if args.status:
        output = {
            "host": monitor.gather_host(config),
            "stations": monitor.gather(config),
        }
        print(json.dumps(output, indent=2, default=str))
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
        monitor.run_loop(config, publisher)
    return 0


if __name__ == "__main__":
    sys.exit(main())
