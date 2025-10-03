import argparse
import logging
import logging.handlers
import os
from mfusepy import FUSE
import sys

from autoortho_fuse import AutoOrtho, fuse_option_profiles_by_os
from aostats import update_process_memory_stat, clear_process_memory_stat

log = logging.getLogger(__name__)


def configure_worker_logging(mount_name, loglevel: str):
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
    else:
        # in addition to console
        from pathlib import Path
        log_dir = Path.home() / ".autoortho-data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(log_dir / f"worker-{mount_name}.log",
                                                maxBytes=10_485_760, backupCount=3)
        root.addHandler(fh)
        root.setLevel(loglevel)
        root.addFilter(AddMount())
        root.info("Worker logging routed to local file")

    # Fallback: local console logging
    logging.basicConfig(
        level=loglevel,
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
    ap.add_argument("--loglevel", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], default="DEBUG")
    args, unknown = ap.parse_known_args()

    configure_worker_logging(args.volname, args.loglevel)

    log.info(f"MOUNT: {args.mountpoint}")
    additional_args = fuse_option_profiles_by_os(args.nothreads, args.volname)

    log.info("Starting FUSE mount")
    log.debug(
            "Loading FUSE with options: %s",
            ", ".join(sorted(map(str, additional_args.keys())))
    )

    try:
        # Initial heartbeat before mounting
        try:
            update_process_memory_stat()
        except Exception:
            pass
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
        try:
            clear_process_memory_stat()
        except Exception:
            pass


if __name__ == "__main__":
    main()
