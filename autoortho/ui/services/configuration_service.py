"""Configuration persistence service."""

from .common import ServiceError, ServiceErrorCode, ServiceResult


class ConfigurationService:
    def __init__(self, config):
        self.config = config

    def persist(self, *, create_missing=True):
        try:
            self.config.save()
            self.config.refresh_derived_paths(
                create_missing=create_missing,
            )
            return ServiceResult(True)
        except OSError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.FILESYSTEM,
                    "Could not save the AutoOrtho configuration.",
                    str(exc),
                )
            )
