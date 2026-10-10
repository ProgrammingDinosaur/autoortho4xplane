"""GitHub release update service."""

from dataclasses import dataclass

import requests

from .common import ServiceError, ServiceErrorCode, ServiceResult


@dataclass(frozen=True)
class UpdateInfo:
    tag: str
    url: str


class UpdateService:
    API_URL = (
        "https://api.github.com/repos/"
        "ProgrammingDinosaur/autoortho4xplane/releases/latest"
    )

    def check(self):
        try:
            response = requests.get(
                self.API_URL,
                timeout=7,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "autoortho4xplane-update-check",
                },
            )
            response.raise_for_status()
            data = response.json()
            return ServiceResult(
                UpdateInfo(
                    str(data.get("tag_name") or data.get("name") or ""),
                    str(
                        data.get("html_url")
                        or "https://github.com/ProgrammingDinosaur/"
                        "autoortho4xplane/releases"
                    ),
                )
            )
        except requests.RequestException as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.NETWORK,
                    "Could not check for updates.",
                    str(exc),
                )
            )
