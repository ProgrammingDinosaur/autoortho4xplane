"""System-readiness service."""

from threading import Event

if __package__ and __package__.startswith("autoortho."):
    from autoortho.ui.readiness import (
        build_readiness,
        detect_xplane_installation,
        infer_setup_complete,
    )
else:
    from ui.readiness import (
        build_readiness,
        detect_xplane_installation,
        infer_setup_complete,
    )

from .common import ServiceError, ServiceErrorCode, ServiceResult


class ReadinessService:
    def check(
        self,
        values,
        scenery_choices=(),
        *,
        cancel_event: Event | None = None,
    ):
        if cancel_event is not None and cancel_event.is_set():
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.CANCELLED,
                    "Readiness check cancelled.",
                )
            )
        try:
            return ServiceResult(build_readiness(values, scenery_choices))
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.VALIDATION,
                    "Readiness checks could not be completed.",
                    str(exc),
                )
            )

    def infer_complete(self, values) -> ServiceResult[bool]:
        try:
            return ServiceResult(infer_setup_complete(values))
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.VALIDATION,
                    "Existing setup could not be verified.",
                    str(exc),
                )
            )

    def detect_xplane(
        self,
        search_roots=None,
        *,
        cancel_event: Event | None = None,
    ) -> ServiceResult[str]:
        try:
            path = detect_xplane_installation(
                search_roots,
                cancel_callback=(
                    cancel_event.is_set
                    if cancel_event is not None
                    else None
                ),
            )
            if cancel_event is not None and cancel_event.is_set():
                return ServiceResult(
                    error=ServiceError(
                        ServiceErrorCode.CANCELLED,
                        "X-Plane detection cancelled.",
                    )
                )
            return ServiceResult(str(path) if path is not None else "")
        except Exception as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "X-Plane detection failed.",
                    str(exc),
                )
            )
