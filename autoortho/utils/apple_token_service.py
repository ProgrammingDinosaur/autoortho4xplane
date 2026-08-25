"""Thread-safe retrieval and renewal of Apple Maps access tokens."""

import threading

import requests


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
        self._refresh_lock = threading.Lock()

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

    def reset_apple_maps_token(self, expected_generation=None) -> str:
        """Refresh once when concurrent requests reject the same generation."""
        with self._refresh_lock:
            if (
                expected_generation is not None
                and self.apple_token is not None
                and self.generation != expected_generation
            ):
                return self.apple_token
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

            self.apple_token = metadata["access_key"]
            self.version = metadata["version"]
            self.generation += 1
            return self.apple_token


apple_token_service = AppleTokenService()
