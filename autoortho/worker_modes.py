import os


MOUNT_WORKER_MODES = {"mount_worker", "macfuse_worker"}


def is_mount_worker_mode(mode=None):
    if mode is None:
        mode = os.environ.get("AO_RUN_MODE")
    return mode in MOUNT_WORKER_MODES
