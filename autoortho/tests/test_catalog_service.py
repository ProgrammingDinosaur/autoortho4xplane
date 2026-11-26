"""The scenery catalog reads GitHub on its worker, once per release.

The window used to read each region's release info while drawing the list,
on its main thread, at every refresh: 297 requests in one flight, and a
timeout there went uncaught.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from autoortho import downloader  # noqa: E402
from autoortho.ui.services.catalog_service import CatalogService  # noqa: E402


class _Region:
    def __init__(self, region_id, release):
        self.region_id = region_id
        self._release = release

    def get_latest_release(self):
        return self._release


class _Release:
    def __init__(self, fails=False):
        self.fails = fails
        self.parsed = False
        self.calls = 0

    def parse(self):
        self.calls += 1
        if self.fails:
            raise TimeoutError("read timed out")
        self.parsed = True


class _Manager:
    def __init__(self, regions):
        self._found = regions
        self.regions = {}

    def find_regions(self):
        self.regions = dict(self._found)


def test_fetch_parses_the_latest_releases_and_survives_one_that_fails():
    good, bad = _Release(), _Release(fails=True)
    manager = _Manager({"na": _Region("na", good), "eur": _Region("eur", bad)})

    result = CatalogService(manager).fetch()

    assert result.success
    assert [r.region_id for r in result.value.regions] == ["na", "eur"]
    assert good.parsed and good.calls == 1
    assert not bad.parsed and bad.calls == 1  # listed all the same


def test_release_info_is_downloaded_once_per_process(tmp_path, monkeypatch):
    requests = []

    def do_url(url, headers={}):
        requests.append(url)
        return b'{"info_ver": "v2", "id": "test"}'

    monkeypatch.setattr(downloader, "do_url", do_url)
    monkeypatch.setattr(downloader, "_release_info_cache", {})
    release_dict = {"assets": [
        {"name": "test_info.json", "size": 10, "download_count": 3,
         "browser_download_url": "https://example.invalid/test_info.json"},
        {"name": "z_test_00.zip", "size": 1000, "download_count": 7,
         "browser_download_url": "https://example.invalid/z_test_00.zip"},
    ]}

    def release():
        return downloader.Release(
            "test", install_dir=str(tmp_path), download_dir=str(tmp_path),
            release_dict=release_dict,
        )

    first, second = release(), release()  # as two refreshes make them
    first.parse()
    second.parse()

    assert requests == ["https://example.invalid/test_info.json"]
    assert second.parsed and second.totalsize == 1010 and second.download_count == 7
