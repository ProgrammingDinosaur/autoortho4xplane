"""Thread-safe retrieval and renewal of Apple Maps access tokens."""

import logging
import threading

import requests

log = logging.getLogger(__name__)


class AppleTokenService:
    """Retrieve Apple Maps tokens while coalescing concurrent refreshes."""

    def __init__(self):
        self.duckduckgo_token_url = (
            "https://duckduckgo.com/local.js?get_mk_token=1"
        )
        self.apple_token_url = (
            "https://cdn.apple-mapkit.com/ma/bootstrap"
            "?apiVersion=2&mkjsVersion=5.79.95&poi=1"
        )
        self.apple_token = None
        self.version = 0
        self.generation = 0
        self._condition = threading.Condition()
        self._refreshing_generation = None
        self._refresh_error = None
        self._invalid_responses = 0

    def snapshot(self, *, wait=True):
        """Return one atomic token generation, optionally waiting for rotation."""
        with self._condition:
            while wait and self._refreshing_generation is not None:
                self._condition.wait()
            return (
                self.apple_token,
                self.version,
                self.generation,
                self._refreshing_generation is None,
            )

    def get_url_metadata_from_response(
        self, response: dict
    ) -> dict[str, str]:
        try:
            for tile_source in response["tileSources"]:
                if tile_source["tileSource"] != "satellite":
                    continue
                path = tile_source["path"]
                return {
                    "version": path.split("v=")[1].split("&")[0],
                    "access_key": path.split("accessKey=")[1].split("&")[0],
                }
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Failed to parse Apple Maps token metadata: {exc}"
            ) from exc
        raise RuntimeError("Apple Maps response contained no satellite source")

    def reset_apple_maps_token(
        self,
        expected_generation=None,
        *,
        status_code=None,
    ) -> str:
        """Refresh once when concurrent requests reject the same generation."""
        with self._condition:
            if (
                expected_generation is not None
                and self.apple_token is not None
                and self.generation != expected_generation
            ):
                return self.apple_token
            self._invalid_responses += 1
            if self._refreshing_generation is not None:
                waiting_generation = self._refreshing_generation
                while self._refreshing_generation is not None:
                    self._condition.wait()
                if (
                    self.generation == waiting_generation
                    and self._refresh_error is not None
                ):
                    raise RuntimeError(
                        "Apple Maps token refresh failed"
                    ) from self._refresh_error
                return self.apple_token
            invalid_generation = self.generation
            self._refreshing_generation = invalid_generation
            self._refresh_error = None

        log.warning(
            "APPLE token generation %d rejected%s; rotating once",
            invalid_generation,
            f" with HTTP {status_code}" if status_code is not None else "",
        )
        try:
            try:
                token_response = requests.get(
                    self.duckduckgo_token_url,
                    timeout=(5, 15),
                )
                token_response.raise_for_status()

                apple_response = requests.get(
                    self.apple_token_url,
                    headers={
                        "Origin": "https://duckduckgo.com",
                        "Authorization": f"Bearer {token_response.text}",
                    },
                    timeout=(5, 15),
                )
                apple_response.raise_for_status()
                metadata = self.get_url_metadata_from_response(
                    apple_response.json()
                )
            except (requests.exceptions.RequestException, ValueError) as exc:
                raise RuntimeError(
                    f"Failed to retrieve Apple Maps token: {exc}"
                ) from exc

            with self._condition:
                rejected = self._invalid_responses
                self.apple_token = metadata["access_key"]
                self.version = metadata["version"]
                self.generation += 1
                self._invalid_responses = 0
                self._refreshing_generation = None
                self._refresh_error = None
                self._condition.notify_all()
            log.warning(
                "APPLE token rotated generation %d -> %d after %d "
                "correlated rejection(s)",
                invalid_generation,
                self.generation,
                rejected,
            )
            return self.apple_token
        except Exception as exc:
            with self._condition:
                self._refresh_error = exc
                self._refreshing_generation = None
                self._condition.notify_all()
            raise


apple_token_service = AppleTokenService()
