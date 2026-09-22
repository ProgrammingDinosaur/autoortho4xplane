"""SimBrief flight-plan service."""

import requests

from .common import ServiceError, ServiceErrorCode, ServiceResult


class SimBriefService:
    API_URL = "https://www.simbrief.com/api/xml.fetcher.php"

    def fetch(self, user_id: str):
        try:
            response = requests.get(
                f"{self.API_URL}?userid={user_id}&json=1",
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            status = str(data.get("fetch", {}).get("status", ""))
            if status.lower().startswith("error"):
                return ServiceResult(
                    error=ServiceError(
                        ServiceErrorCode.UNAVAILABLE,
                        status,
                    )
                )
            return ServiceResult(data)
        except requests.Timeout as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.NETWORK,
                    "The SimBrief request timed out.",
                    str(exc),
                )
            )
        except requests.RequestException as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.NETWORK,
                    "Could not fetch SimBrief flight data.",
                    str(exc),
                )
            )
        except ValueError as exc:
            return ServiceResult(
                error=ServiceError(
                    ServiceErrorCode.UNAVAILABLE,
                    "SimBrief returned an invalid response.",
                    str(exc),
                )
            )
