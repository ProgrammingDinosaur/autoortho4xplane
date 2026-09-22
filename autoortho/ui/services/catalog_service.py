"""Scenery catalog service."""

from dataclasses import dataclass
from threading import Event

from .common import ServiceError, ServiceErrorCode, ServiceResult


@dataclass(frozen=True)
class CatalogSnapshot:
    regions: tuple


class CatalogService:
    def __init__(self, manager):
        self.manager = manager

    def fetch(
        self,
        *,
        cancel_event: Event | None = None,
    ) -> ServiceResult[CatalogSnapshot]:
        if cancel_event is not None and cancel_event.is_set():
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.CANCELLED,
                    "Catalog refresh cancelled.",
                )
            )
        try:
            self.manager.find_regions()
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.NETWORK,
                    "Could not refresh the scenery catalog.",
                    str(exc),
                )
            )
        if cancel_event is not None and cancel_event.is_set():
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.CANCELLED,
                    "Catalog refresh cancelled.",
                )
            )
        return ServiceResult(
            CatalogSnapshot(tuple(self.manager.regions.values()))
        )
