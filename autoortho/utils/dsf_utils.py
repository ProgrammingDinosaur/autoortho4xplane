import logging
import os
import subprocess
import re

from constants import DSFTOOL_PATH

log = logging.getLogger(__name__)

def get_ter_tiles_from_dsf(dsf_path: str):
    """ get the ter tiles from the dsf """
    if not os.path.exists(DSFTOOL_PATH):
        log.error("DSFTOOL_PATH does not exist")
        return []
    # command looks like: .\DSFTool --dsf2text <name>.dsf -
    # Stream stdout line-by-line to avoid loading the entire output into memory.
    command = [DSFTOOL_PATH, "--dsf2text", dsf_path, "-"]

    final_ter_tiles = set()
    found_ter_tiles = False
    stopped_early = False

    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if "TERRAIN_DEF terrain/" in line:
                if "overlay" in line:
                    continue
                found_ter_tiles = True
                parsed_line = line.replace("TERRAIN_DEF terrain/", "").replace(".ter", "").strip()
                final_ter_tiles.add(parsed_line)
            elif found_ter_tiles:
                # We have passed the TERRAIN_DEF block; stop early
                stopped_early = True
                break
    finally:
        # If we stopped early, terminate the process to avoid processing the rest of the DSF
        if stopped_early and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        # Ensure the process is reaped
        try:
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    return list(final_ter_tiles)