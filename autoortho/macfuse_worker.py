import argparse
import logging
import logging.handlers
import os
from mfusepy import FUSE
import sys

from autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os

log = logging.getLogger(__name__)


def configure_worker_logging(mount_name):
    addr = os.getenv("AO_LOG_ADDR")

    # A filter that annotates every record with the mount id
    class AddMount(logging.Filter):
        def filter(self, record):
            record.mount = mount_name
            return True

    root = logging.getLogger()
    # First try socket logging to the parent
    if addr:
        host, port = addr.split(":")
        try:
            # set format to include mount name
            logging.basicConfig(
                format='[WORKER %(process)d][%(mount)s]: %(message)s',
                stream=sys.stdout
            )
            sh = logging.handlers.SocketHandler(host, int(port))
            # Replace any existing handlers with the socket handler
            root.handlers[:] = []
            root.addHandler(sh)
            root.setLevel(logging.INFO)
            root.addFilter(AddMount())
            root.info("Worker logging routed to parent via SocketHandler")
            return
        except Exception as e:
            # fall back to local console if socket setup fails
            log.error(f"Worker logging routed to parent via SocketHandler failed: {e}")

    # Fallback: local console logging
    logging.basicConfig(
        level=logging.INFO,
        format='[WORKER %(process)d][%(mount)s]: %(message)s',
        stream=sys.stdout
    )
    root.addFilter(AddMount())


def main():

    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--mountpoint", required=True)
    ap.add_argument("--nothreads", action="store_true")
    ap.add_argument("--volname")
    args = ap.parse_args()

    configure_worker_logging(args.volname)

    log.info(f"MOUNT: {args.mountpoint}")
    additional_args = fuse_option_profiles_by_os(args.nothreads, args.volname)

    log.info("Starting FUSE mount")
    log.debug(
            "Loading FUSE with options: %s",
            ", ".join(sorted(map(str, additional_args.keys())))
    )

    try:
        FUSE(AutoOrtho(args.root, use_ns=True), os.path.abspath(args.mountpoint), **additional_args)
        log.info(f"FUSE: Exiting mount {args.mountpoint}")
    except Exception as e:
        log.error(f"FUSE mount failed with non-negotiable error: {e}")
        raise
    finally:
        try:
            from getortho import stats_batcher, shutdown
            if stats_batcher:
                stats_batcher.stop()
            shutdown()
        except Exception as e:
            log.error(f"Error stopping stats batcher: {e}")
            pass


if __name__ == "__main__":
    main()
