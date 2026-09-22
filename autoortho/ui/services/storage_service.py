"""Filesystem capacity and directory-size service."""

from dataclasses import dataclass
from threading import Event

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import (
        free_space_bytes,
        recursive_directory_usage_bytes,
    )
else:
    from ui.readiness import free_space_bytes, recursive_directory_usage_bytes

from .common import ServiceError, ServiceErrorCode, ServiceResult


@dataclass(frozen=True)
class StorageSummary:
    path: str
    used_bytes: int
    free_bytes: int


@dataclass(frozen=True)
class InstallationCapacity:
    download_free_bytes: int
    destination_free_bytes: int
    temporary_required_bytes: int
    final_required_bytes: int

    @property
    def sufficient(self) -> bool:
        return (
            self.download_free_bytes >= self.temporary_required_bytes
            and self.destination_free_bytes >= self.final_required_bytes
        )


class StorageService:
    def inspect(
        self,
        path: str,
        *,
        cancel_event: Event | None = None,
    ) -> ServiceResult[StorageSummary]:
        try:
            used = recursive_directory_usage_bytes(
                path,
                cancel_callback=(
                    cancel_event.is_set if cancel_event is not None else None
                ),
            )
            if cancel_event is not None and cancel_event.is_set():
                return ServiceResult(
                    error=ServiceError(
                        ServiceErrorCode.CANCELLED,
                        "Storage scan cancelled.",
                    )
                )
            free = free_space_bytes(path)
            return ServiceResult(StorageSummary(path, used, free))
        except OSError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "Could not inspect storage.",
                    str(exc),
                )
            )

    def check_installation_capacity(
        self,
        download_path: str,
        destination_path: str,
        temporary_required: int,
        final_required: int,
        *,
        cancel_event: Event | None = None,
    ) -> ServiceResult[InstallationCapacity]:
        if cancel_event is not None and cancel_event.is_set():
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.CANCELLED,
                    "Installation storage check cancelled.",
                )
            )
        try:
            download_free = free_space_bytes(download_path)
            if cancel_event is not None and cancel_event.is_set():
                return ServiceResult(
                    error=ServiceError(
                        ServiceErrorCode.CANCELLED,
                        "Installation storage check cancelled.",
                    )
                )
            destination_free = free_space_bytes(destination_path)
            return ServiceResult(
                InstallationCapacity(
                    download_free_bytes=download_free,
                    destination_free_bytes=destination_free,
                    temporary_required_bytes=int(temporary_required),
                    final_required_bytes=int(final_required),
                )
            )
        except OSError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "Could not inspect installation storage.",
                    str(exc),
                )
            )
