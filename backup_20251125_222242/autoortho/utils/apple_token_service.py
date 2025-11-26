"""Module for handling retrieval of Apple Map access tokens"""

from re import A
import requests

class AppleTokenService:
    """Service for retrieving and renewing Apple Map access tokens"""
    def __init__(self):
        self.duckduckgo_token_url = "https://duckduckgo.com/local.js?get_mk_token=1"
        self.apple_token_url = "https://cdn.apple-mapkit.com/ma/bootstrap?apiVersion=2&mkjsVersion=5.79.95&poi=1"
        self.apple_token = None
        self.version = 0

    def get_url_metadata_from_response(self, response: dict) -> dict[str, str]:
        """Process the Apple Maps access token"""
        try:
            tile_sources = response["tileSources"]
            for tile_source in tile_sources:
                if tile_source["tileSource"] == "satellite":
                    path = tile_source["path"]
                    version = path.split("v=")[1].split("&")[0]
                    access_key = path.split("accessKey=")[1].split("&")[0]
                    return {
                        "version": version,
                        "access_key": access_key
                    }
        except Exception as e:
            raise RuntimeError(f"Failed to get URL metadata from response: {e}")

    def reset_apple_maps_token(self) -> str:
        """
        Retrieve Apple Maps access token
        This is a weird and morally questionable workaround to get the token.
        But it's functional.
        """
        try:
            dd_go_token_response = requests.get(self.duckduckgo_token_url)
            dd_go_token_response.raise_for_status()
            dd_go_token = dd_go_token_response.text

            apple_token_response = requests.get(
                url=self.apple_token_url,
                headers={
                    "Origin": "https://duckduckgo.com",
                    "Authorization": f"Bearer {dd_go_token}",
                },
            )
            apple_token_response.raise_for_status()
            apple_token_body = apple_token_response.json()
            url_metadata = self.get_url_metadata_from_response(apple_token_body)

            self.apple_token = url_metadata["access_key"]
            self.version = url_metadata["version"]

        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Failed to retrieve Apple Maps token: {e}")

apple_token_service = AppleTokenService()