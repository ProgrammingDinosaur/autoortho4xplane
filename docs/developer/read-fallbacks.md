# Read fallbacks: what X-Plane gets when a texture read can't wait

A user report (AutoOrtho 3.4.2, XP12 streaming, Arrabury) showed whole 1x1
degree tiles white. X-Plane reads each texture once and keeps what it got until
it loads the texture again; reload scenery re-opens the files but reads nothing.
Whatever AutoOrtho answers a failed read with therefore stays on screen.

## When the FUSE layer falls back

`AutoOrtho._read` ([autoortho_fuse.py](../../autoortho/autoortho_fuse.py))
answers a texture read with `_fallback_dds` instead of an I/O error (which
crashes X-Plane on Windows) when:

| Reason (`fuse_fallback_<reason>`) | Cause |
|---|---|
| `lock_timeout` | the read waited for its tile longer than the read budget + `fallback_timeout` + 15 s |
| `admission_timeout` | no live build slot freed up in that time (memory-weighted gate) |
| `read_none`, `fuse_error`, `exception` | the tile read failed |
| `direct_failed` | a texture served from its cache file failed and could not switch to a live tile |
| `shutdown` | AutoOrtho is stopping |
| `injected` | the debug switch below |

Every fallback is counted in STATS, and `fuse_fallback_imagery` counts those
served from the tile itself.

## What a fallback serves

1. **The tile's own bytes** when the tile is in memory and matches the size
   X-Plane was told (`Tile.standin_bytes`): its real header, what is built,
   unbuilt levels upscaled from the nearest coarser built level
   (`DDS.read_served`), missing color only where nothing covers. No download,
   build or wait; it gives up after 0.25 s on a busy tile lock.
2. **Otherwise a placeholder** ([dds_fallback.py](../../autoortho/dds_fallback.py)):
   the header pydds would write for that size, then solid `missing_color`
   blocks. Until 3.4.3 the header was all zeros; X-Plane rejected the texture
   ("some scenery textures could not be loaded") and drew it white.

## Eviction that frees nothing

The same flight's worker dropped 20 tiles every 1.4 s with 0 MB freed and
rebuilt 1,250 tiles X-Plane reopened right after. Tile eviction compares the
processes' memory with the cache limit, and much of that memory is not tiles,
so a low limit (the hardware baseline gives 3 GB on a 12 GB PC) can set a
target eviction never reaches. `EvictionStallGuard`
([eviction_guard.py](../../autoortho/eviction_guard.py)) pauses memory
eviction after three passes that free under 16 MB (30 s, doubling to 5 min),
resumes early when memory grows 5 % past where it stalled, and warns once
(counter `evict_stalled`). Eviction by tile count is unchanged.

## Testing in X-Plane

`AO_DEBUG_FAIL_DDS` ([debug_faults.py](../../autoortho/debug_faults.py)) makes
reads fail on purpose: `all` or DSF-style tiles (`+62-008,+55+012`);
`AO_DEBUG_FAIL_DDS_SECONDS=60` limits it to the first minute after each tile's
first read; `AO_DEBUG_FAIL_DDS_HEADER=zero` restores the old header to
reproduce the white tiles. Verified at Vágar: zero header gives white tiles and
the X-Plane warning, a valid header gives missing color, and imagery returns
after an X-Plane restart (not a scenery reload) once the window has closed.

## Considered and already covered

- **Header reads that never fail**: every fallback now carries a valid header,
  the tile's own when it is in memory; header chunks already skip admission.
- **Lower zoom on low-RAM PCs**: the packaged `max_zoom` is 16; ZL17 is a
  user's choice, which the RAM and graphics-memory guards already check.
- **Disk-cache strain**: the dropped writes in that log were the long-term
  backup copy only; the DDS cache store failures are fixed by
  `4cbe69ea` and per-chunk files are replaced by the chunk store.
