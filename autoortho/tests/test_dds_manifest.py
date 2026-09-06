import json
import os
import threading

from autoortho import pydds
from autoortho.aopipeline.dynamic_dds_cache import (
    DDM_VERSION,
    DynamicDDSCache,
)
from autoortho.dds_manifest import (
    CompressedRowResult,
    MipmapSource,
    RowBuildManifest,
    RowState,
    RowStateCoordinator,
    manifests_from_dict,
    manifests_to_dict,
)


def _result(row, generation, sources, data, tile_id="0_0_BI_16"):
    manifest = RowBuildManifest.create(
        tile_id=tile_id,
        target_zoom=17,
        mipmap=0,
        row_index=row,
        build_generation=generation,
        sources=sources,
        compressed_data=data,
    )
    return CompressedRowResult(data, manifest)


def test_row_manifest_freezes_mixed_compression_sources():
    mutable_sources = bytearray(
        [
            MipmapSource.TARGET_PROVIDER,
            MipmapSource.LOWER_ZL_CACHE,
            MipmapSource.MISSING_COLOR,
        ]
    )
    result = _result(0, 4, mutable_sources, b"compressed")
    mutable_sources[:] = bytes([MipmapSource.TARGET_PROVIDER]) * 3

    assert not result.manifest.is_exact
    assert result.manifest.fallback_indices == (1,)
    assert result.manifest.missing_indices == (2,)
    assert result.manifest.source_counts == {
        MipmapSource.TARGET_PROVIDER: 1,
        MipmapSource.LOWER_ZL_CACHE: 1,
        MipmapSource.MISSING_COLOR: 1,
    }


def test_manifest_round_trip_preserves_checksum_and_sources():
    result = _result(
        3,
        7,
        bytes([MipmapSource.TARGET_PROVIDER]) * 4,
        b"row-data",
    )

    encoded = manifests_to_dict(
        {3: result.manifest},
        target_zoom=17,
    )
    decoded = manifests_from_dict(encoded, tile_id="0_0_BI_16")

    assert decoded == {3: result.manifest}
    assert decoded[3].validate_data(b"row-data")
    assert not decoded[3].validate_data(b"changed")


def test_first_serve_seals_generation_against_late_exact_commit():
    coordinator = RowStateCoordinator(1)
    degraded = _result(
        0,
        1,
        bytes([MipmapSource.LOWER_MIPMAP_MEMORY]) * 2,
        b"degraded",
    )
    installed = []
    assert coordinator.commit((degraded,), lambda rows: installed.extend(rows))

    data, manifests = coordinator.seal_and_read(
        (0,),
        lambda: installed[0].data,
        lambda _row: None,
    )
    exact = _result(
        0,
        2,
        bytes([MipmapSource.TARGET_PROVIDER]) * 2,
        b"exact",
    )

    assert data == b"degraded"
    assert manifests == (degraded.manifest,)
    assert coordinator.state(0) == RowState.SERVED_DEGRADED
    assert not coordinator.commit((exact,), lambda rows: installed.extend(rows))
    assert installed == [degraded]


def test_concurrent_readers_observe_one_committed_manifest():
    coordinator = RowStateCoordinator(1)
    result = _result(
        0,
        9,
        bytes([MipmapSource.TARGET_PROVIDER]) * 2,
        b"exact",
    )
    assert coordinator.commit((result,), lambda _rows: None)
    observed = []

    def read():
        observed.append(
            coordinator.seal_and_read(
                (0,),
                lambda: result.data,
                lambda _row: None,
            )
        )

    readers = [threading.Thread(target=read) for _ in range(8)]
    for reader in readers:
        reader.start()
    for reader in readers:
        reader.join()

    assert observed == [(b"exact", (result.manifest,))] * 8
    assert coordinator.state(0) == RowState.SERVED_EXACT


class _Tile:
    def __init__(self):
        self.row = 0
        self.col = 0
        self.maptype = "BI"
        self.tilename_zoom = 16
        self.max_zoom = 17
        self.id = "0_0_BI_16"
        self.dds = pydds.DDS(512, 512, dxt_format="BC1")
        self.chunks_per_row = 2
        self.chunks_per_col = 2
        self._dds_needs_healing = False
        self._dds_missing_indices = []
        self._dds_fallback_indices = []
        self._loaded_mm0_manifests = {}


def _complete_dds(tile):
    dds_bytes = tile.dds.read_at(0, tile.dds.total_size)
    mm0 = tile.dds.mipmap_list[0]
    row_bytes = mm0.length // tile.chunks_per_col
    manifests = {}
    for row in range(tile.chunks_per_col):
        start = mm0.startpos + row * row_bytes
        row_data = dds_bytes[start:start + row_bytes]
        manifests[row] = _result(
            row,
            1,
            bytes([MipmapSource.TARGET_PROVIDER])
            * tile.chunks_per_row,
            row_data,
            tile.id,
        ).manifest
    return dds_bytes, manifests


def test_v5_cache_round_trip_requires_valid_exact_manifests(tmp_path):
    cache = DynamicDDSCache(str(tmp_path), max_size_mb=0)
    tile = _Tile()
    dds_bytes, manifests = _complete_dds(tile)
    try:
        assert not cache.store(tile.id, tile.max_zoom, dds_bytes, tile)
        assert cache.store(
            tile.id,
            tile.max_zoom,
            dds_bytes,
            tile,
            mm0_manifest=manifests,
        )

        loaded_tile = _Tile()
        assert cache.load(
            loaded_tile.id,
            loaded_tile.max_zoom,
            loaded_tile,
        ) == dds_bytes
        assert loaded_tile._loaded_mm0_manifests == manifests
    finally:
        cache.close()


def test_v4_compiled_entry_is_deleted_without_touching_jpegs(tmp_path):
    cache = DynamicDDSCache(str(tmp_path), max_size_mb=0)
    tile = _Tile()
    dds_path, ddm_path = cache._paths_for(
        tile.row,
        tile.col,
        tile.maptype,
        tile.tilename_zoom,
        tile.max_zoom,
    )
    jpeg_path = tmp_path / "0_0_17_BI.jpg"
    jpeg_path.write_bytes(b"jpeg")
    try:
        os.makedirs(os.path.dirname(dds_path), exist_ok=True)
        with open(dds_path, "wb") as dds_file:
            dds_file.write(b"D" * tile.dds.total_size)
        with open(ddm_path, "w", encoding="utf-8") as metadata_file:
            json.dump({"v": 4}, metadata_file)

        assert cache.load(tile.id, tile.max_zoom, tile) is None
        assert jpeg_path.exists()
        assert not os.path.exists(dds_path)
        assert not os.path.exists(ddm_path)
        assert DDM_VERSION == 5
    finally:
        cache.close()
