"""Typed mount-control boundary."""

from dataclasses import dataclass

from .common import ServiceError, ServiceErrorCode, ServiceResult


@dataclass(frozen=True)
class MountOperation:
    action: str
    success: bool
    message: str


class MountService:
    def __init__(self, controller):
        self.controller = controller

    def start(self):
        try:
            success = bool(
                self.controller.mount_sceneries(blocking=False)
            )
            return ServiceResult(
                MountOperation(
                    "start",
                    success,
                    (
                        "Streaming started."
                        if success
                        else "Scenery mounts could not be started."
                    ),
                )
            )
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.INTERNAL,
                    "Scenery streaming could not be started.",
                    str(exc),
                )
            )

    def stop(self):
        try:
            success = bool(self.controller.unmount_sceneries())
            return ServiceResult(
                MountOperation(
                    "stop",
                    success,
                    (
                        "Streaming stopped."
                        if success
                        else "One or more scenery mounts remain active."
                    ),
                )
            )
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.INTERNAL,
                    "Scenery streaming could not be stopped.",
                    str(exc),
                )
            )
