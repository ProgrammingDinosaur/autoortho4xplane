"""Scenery catalog service."""

import logging
from dataclasses import dataclass
from threading import Event

from .common import ServiceError, ServiceErrorCode, ServiceResult

log = logging.getLogger(__name__)


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
        # Each region's latest release reads its info file from GitHub. That
        # belongs here, on the worker: the window used to do it while drawing
        # the list, on its main thread, where a slow answer froze it and a
        # timeout went uncaught. A region whose info cannot be read is still
        # listed, with what the release list says about it.
        for region in list(self.manager.regions.values()):
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                region.get_latest_release().parse()
            except Exception as exc:
                log.warning(
                    "Could not read the release info of %s: %s",
                    getattr(region, "region_id", region), exc,
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
