"""Background workers used by the Qt controller."""

import logging
import os
import threading
import traceback

from PySide6.QtCore import QThread, Signal

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.services import (
        MountService,
        SimBriefService,
        StorageService,
        UpdateService,
    )
    from autoortho.utils.dsf_utils import dsf_utils
else:
    from ui.services import (
        MountService,
        SimBriefService,
        StorageService,
        UpdateService,
    )
    from utils.dsf_utils import dsf_utils

log = logging.getLogger(__name__)


class SceneryDownloadWorker(QThread):
    progress = Signal(str, dict)
    finished = Signal(str, bool)
    error = Signal(str, str)

    def __init__(self, dl_manager, region_id, download_dir):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id
        self.download_dir = download_dir

    def run(self):
        try:
            self.dl_manager.download_dir = self.download_dir
            region = self.dl_manager.regions.get(self.region_id)
            success = region.install_release(
                progress_callback=lambda payload: self.progress.emit(
                    self.region_id,
                    payload,
                ),
                noclean=self.dl_manager.noclean,
            )
            self.finished.emit(self.region_id, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.region_id, str(exc))


class SceneryUninstallWorker(QThread):
    finished = Signal(str, bool)
    error = Signal(str, str)

    def __init__(self, dl_manager, region_id):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id

    def run(self):
        try:
            success = self.dl_manager.regions[
                self.region_id
            ].local_rel.uninstall()
            self.finished.emit(self.region_id, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.region_id, str(exc))


class UpdateCheckWorker(QThread):
    result = Signal(object)
    error = Signal(str)

    def __init__(self):
        super().__init__()
        self.service = UpdateService()

    def cancel(self):
        self.requestInterruption()

    def run(self):
        if self.isInterruptionRequested():
            return
        result = self.service.check()
        if self.isInterruptionRequested():
            return
        if result.success:
            info = result.value
            self.result.emit((info.tag, info.url) if info else None)
        else:
            self.error.emit(result.error.message)


class AddSeasonsWorker(QThread):
    finished = Signal(str, bool)
    error = Signal(str, str)
    progress = Signal(str, dict)

    def __init__(self, scenery_name: str, scenery_path: str):
        super().__init__()
        self.scenery_name = scenery_name
        self.scenery_path = os.path.join(
            scenery_path,
            "z_autoortho",
            "scenery",
            scenery_name,
        )

    def run(self):
        try:
            success = dsf_utils.add_seasons_to_package(
                self.scenery_name,
                progress_callback=lambda payload: self.progress.emit(
                    self.scenery_name,
                    payload,
                ),
            )
            self.progress.emit(self.scenery_name, {"stage": "finished"})
            self.finished.emit(self.scenery_name, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.scenery_name, str(exc))


class RestoreDefaultDsfsWorker(QThread):
    finished = Signal(str, bool)
    error = Signal(str, str)
    progress = Signal(str, dict)

    def __init__(self, dl_manager, region_id):
        super().__init__()
        self.dl_manager = dl_manager
        self.region_id = region_id

    def run(self):
        try:
            success = dsf_utils.restore_default_dsfs(
                self.region_id,
                progress_callback=lambda payload: self.progress.emit(
                    self.region_id,
                    payload,
                ),
            )
            self.finished.emit(self.region_id, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.region_id, str(exc))


class AddRoughnessWorker(QThread):
    finished = Signal(str, bool)
    error = Signal(str, str)
    progress = Signal(str, dict)

    def __init__(self, scenery_name: str, scenery_path: str, roughness_value):
        super().__init__()
        self.scenery_name = scenery_name
        self.scenery_path = scenery_path
        self.roughness_value = roughness_value

    def run(self):
        try:
            if __package__ and __package__.startswith("autoortho."):
                from autoortho.utils.ter_utils import ter_utils
            else:
                from utils.ter_utils import ter_utils
            self.progress.emit(self.scenery_name, {"stage": "scanning"})
            success = ter_utils.patch_terrain_to_package(
                self.scenery_name,
                self.roughness_value,
                progress_callback=lambda payload: self.progress.emit(
                    self.scenery_name,
                    payload,
                ),
            )
            self.progress.emit(self.scenery_name, {"stage": "finished"})
            self.finished.emit(self.scenery_name, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.scenery_name, str(exc))


class RestoreRoughnessWorker(QThread):
    finished = Signal(str, bool)
    error = Signal(str, str)
    progress = Signal(str, dict)

    def __init__(self, scenery_name: str, scenery_path: str):
        super().__init__()
        self.scenery_name = scenery_name
        self.scenery_path = scenery_path

    def run(self):
        try:
            if __package__ and __package__.startswith("autoortho."):
                from autoortho.utils.ter_utils import ter_utils
            else:
                from utils.ter_utils import ter_utils
            success = ter_utils.restore_ter_files(
                self.scenery_name,
                progress_callback=lambda payload: self.progress.emit(
                    self.scenery_name,
                    payload,
                ),
            )
            self.progress.emit(self.scenery_name, {"stage": "finished"})
            self.finished.emit(self.scenery_name, bool(success))
        except Exception as exc:
            log.error(traceback.format_exc())
            self.error.emit(self.scenery_name, str(exc))


class SimBriefFetchWorker(QThread):
    success = Signal(dict)
    error = Signal(str)

    def __init__(self, user_id):
        super().__init__()
        self.user_id = user_id
        self.service = SimBriefService()

    def cancel(self):
        self.requestInterruption()

    def run(self):
        if self.isInterruptionRequested():
            return
        result = self.service.fetch(self.user_id)
        if self.isInterruptionRequested():
            return
        if result.success:
            self.success.emit(result.value)
        else:
            self.error.emit(result.error.message)


class MountControlWorker(QThread):
    completed = Signal(str, bool, str)

    def __init__(self, controller, action, lingering_mounts=None):
        super().__init__(controller)
        self.controller = controller
        self.action = action
        self.lingering_mounts = list(lingering_mounts or [])

    def run(self):
        if self.action == "start" and self.lingering_mounts:
            try:
                self.controller.cleanup_lingering_mounts(
                    self.lingering_mounts
                )
            except Exception as exc:
                self.completed.emit("start", False, str(exc))
                return
        service = MountService(self.controller)
        result = service.start() if self.action == "start" else service.stop()
        if result.success:
            operation = result.value
            self.completed.emit(
                operation.action,
                operation.success,
                operation.message,
            )
        else:
            self.completed.emit(
                self.action,
                False,
                result.error.message,
            )


class StorageScanWorker(QThread):
    completed = Signal(str, object, object)

    def __init__(self, cache_path):
        super().__init__()
        self.cache_path = str(cache_path)
        self.service = StorageService()

    def run(self):
        result = self.service.inspect(
            self.cache_path,
            cancel_event=_InterruptionEvent(self),
        )
        if result.success:
            summary = result.value
            self.completed.emit(
                summary.path,
                summary.used_bytes,
                summary.free_bytes,
            )


class CacheCleanupWorker(QThread):
    progress = Signal(str)
    completed = Signal(bool, str, bool)

    def __init__(self, controller, mode, target_size_gb=0):
        super().__init__(controller)
        self.controller = controller
        self.mode = mode
        self.target_size_gb = target_size_gb
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            if self.mode == "jpeg":
                result = self.controller.clean_jpegs_only(
                    self.controller.cfg.paths.cache_dir,
                    cancel_event=self._cancel_event,
                    progress_callback=self.progress.emit,
                )
            else:
                result = self.controller.clean_cache(
                    self.controller.cfg.paths.cache_dir,
                    self.target_size_gb,
                    cancel_event=self._cancel_event,
                    progress_callback=self.progress.emit,
                )
            self.completed.emit(*result)
        except Exception as exc:
            log.error(traceback.format_exc())
            self.completed.emit(False, str(exc), False)


class _InterruptionEvent:
    def __init__(self, worker):
        self.worker = worker

    def is_set(self):
        return self.worker.isInterruptionRequested()
