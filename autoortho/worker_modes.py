import os


MOUNT_WORKER_MODES = {"mount_worker", "macfuse_worker"}
FILES_WORKER_MODE = "files_worker"
# Every child process the parent supervises. They never write the config
# file and route their logs to the parent.
WORKER_MODES = MOUNT_WORKER_MODES | {FILES_WORKER_MODE}


def is_mount_worker_mode(mode=None):
    """True in any supervised worker process (FUSE mounts and files mode).

    Kept under its historical name: every caller means "not the parent".
    """
    if mode is None:
        mode = os.environ.get("AO_RUN_MODE")
    return mode in WORKER_MODES


def is_files_worker_mode(mode=None):
    if mode is None:
        mode = os.environ.get("AO_RUN_MODE")
    return mode == FILES_WORKER_MODE
