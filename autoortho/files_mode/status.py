"""Files mode in plain words, for Home's card (pure: no Qt)."""

GB = 1024 ** 3


def files_mode_status(stats=None, error=None, running=False):
    """``{"headline", "detail", "state"}`` for Home's files-mode card."""
    if error:
        return {"headline": "Files mode could not start", "detail": str(error),
                "state": "error"}
    if not running:
        return {
            "headline": "Ready: no virtual file system needed",
            "detail": ("Textures are written to real files ahead of X-Plane. "
                       "X-Plane waits at a flight's start until the area is built."),
            "state": "info",
        }
    stats = stats or {}
    used = float(stats.get("files_disk_used_bytes") or 0) / GB
    budget = float(stats.get("files_disk_budget_bytes") or 0) / GB
    disk = f"{used:.1f} of {budget:.0f} GB" if budget else f"{used:.1f} GB"
    plugin = "" if stats.get("files_plugin_seen") else " · X-Plane plugin not seen yet"
    if stats.get("files_relocating"):
        built, total = stats.get("files_start_built", 0), stats.get("files_start_total", 0)
        return {"headline": f"Aircraft moved: building the new area ({built} of {total})",
                "detail": f"X-Plane stays paused until it is built · disk {disk}",
                "state": "info"}
    if stats.get("files_starting"):
        built, total = stats.get("files_start_built", 0), stats.get("files_start_total", 0)
        return {"headline": f"Flight start: building {built} of {total} textures",
                "detail": f"X-Plane waits until the start area is built · disk {disk}{plugin}",
                "state": "info"}
    late = int(stats.get("files_late") or 0)
    queued = int(stats.get("files_queue_depth") or 0)
    rate = float(stats.get("files_build_rate") or 0.0) * 60.0
    refused = int(stats.get("files_replace_refused") or 0)
    parts = [f"{queued} queued", f"{rate:.0f} built/min", f"disk {disk}"]
    if stats.get("files_night"):
        parts.append("night: X-Plane's scenery")
    if refused:
        parts.append(f"{refused} replacements refused")
    if stats.get("files_search_indexed"):
        parts.append("Windows Search indexes the files folder (Settings > Files)")
    detail = " · ".join(parts) + plugin
    if late:
        return {"headline": f"{late} textures late (grey or blurry until X-Plane reads them again)",
                "detail": detail, "state": "warning"}
    return {"headline": "Keeping ahead of X-Plane", "detail": detail, "state": "ok"}
