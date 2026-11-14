"""Files-mode health checks (the files-mode counterpart of FUSE's diagnose)."""

import logging
import os
import socket

from . import dds_layout, fsops, plugin_install

log = logging.getLogger(__name__)


def check(pack, gate_port=None, xplane_path=None, timeout=0.5):
    """``[problem, ...]``; empty when files mode looks healthy."""
    problems = []
    for name in pack.links_ok():
        problems.append(f"Custom Scenery/{name} is not linked to the files folder")
    for name in pack.regions:
        textures = pack.textures_dir(name)
        sample = None
        try:
            with os.scandir(textures) as entries:
                for entry in entries:
                    if entry.name.lower().endswith(".dds"):
                        sample = entry.path
                        break
        except OSError as exc:
            problems.append(f"{textures} unreadable: {exc}")
            continue
        if sample is not None:
            try:
                fd = fsops.open_ro(sample)
                try:
                    head = fsops.pread(fd, dds_layout.HEADER_SIZE, 0)
                finally:
                    os.close(fd)
                if head[:4] != b"DDS ":
                    problems.append(f"{sample} is not a DDS file")
            except OSError as exc:
                problems.append(f"{sample} unreadable: {exc}")
    if gate_port:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(b"AO1 HELLO diagnose 0", ("127.0.0.1", int(gate_port)))
            data, _ = sock.recvfrom(64)
            if not data.startswith(b"AO1 ACK"):
                problems.append("The files-mode gate answered oddly")
        except OSError:
            problems.append(f"The files-mode gate does not answer on port {gate_port}")
        finally:
            sock.close()
    if xplane_path and not plugin_install.installed(xplane_path):
        problems.append("The AutoOrtho X-Plane plugin is not installed: X-Plane will not "
                        "wait for the start area to be built")
    for problem in problems:
        log.warning("Files mode check: %s", problem)
    if not problems:
        log.info("Files mode checks passed")
    return problems
