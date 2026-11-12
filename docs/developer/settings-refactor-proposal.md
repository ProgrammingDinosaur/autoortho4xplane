# Settings refactor proposal: Target, Limits, Governor

Status: proposal / draft for discussion (revision 2)
Scope: user-facing settings, how settings are stored and resolved, the setup wizard, and a live auto-tuning engine
Reference studied: OrthoStreamXP (OSXP), shared with permission. This proposal borrows its measurement ideas but keeps AutoOrtho's own model.

---

## 1. Summary

AutoOrtho asks beginners to set about 89 controls across 148 config keys. Most of those controls set *how the engine works* (workers, budgets, buffers, broker limits) rather than *what the user wants*. Users copy values from forum posts that were tuned for someone else's PC, overload their machine, and blame AutoOrtho.

This proposal splits settings into three things the user can reason about, plus one thing the user never has to set:

| | Who owns it | What it is |
|---|---|---|
| **Target** | User | *What I want to see.* An imagery zoom level, either **fixed** or **by altitude** (the existing dynamic zoom steps), with a separate airport zoom. |
| **Limits** | User (auto by default) | *What I allow AutoOrtho to use.* RAM, graphics memory, download bandwidth, CPU share, disk. Detected automatically; the user can lower any of them. |
| **Guards** | AutoOrtho | *Protection.* RAM/VRAM/disk/bandwidth checks before a flight and emergency brakes during it. They warn and offer a fix, but never block. |
| **Governor** | AutoOrtho | *How to get there.* A background control loop that adjusts every engine setting **live, during the flight**, to deliver the target as closely and as quickly as the machine, the network and the limits allow. It remembers what worked for next time. |

What makes this AutoOrtho's own rather than a copy of OSXP:

- **The user keeps the zoom-level vocabulary** and the altitude-based profiles that experienced users value. They are not replaced by named presets.
- **Tuning is continuous, not post-flight.** OSXP suggests one change after each flight and waits for the user's OK. AutoOrtho adjusts within seconds, during the flight, inside limits the user set. An *Advise* mode keeps the OSXP-style "suggest, you apply" behaviour for users who prefer it.
- **Everything the governor does is visible.** A live panel shows target vs delivered quality, the current bottleneck, every adjusted value, its allowed range, and the reason for its last change. Any value can be pinned so the governor leaves it alone.
- **Resource limits are a first-class setting.** The user can say "never use more than 8 GB RAM or 100 Mbit/s", and the governor treats that as a hard ceiling.
- **A new Home page (§13) shows it all working:** quality delivered against the target, resource use against limits, what AutoOrtho is doing ahead of the aircraft, and what the tuner changed. The layout changes with the state (stopped, loading, flying, after flight) and keeps engine internals off the page.

---

## 2. What we take from OSXP, and what we do differently

| OSXP idea | AutoOrtho adopts? | AutoOrtho version |
|---|---|---|
| One quality knob (`level-change`) that hides zoom levels | **No** | The user picks a target zoom level (fixed or altitude profile). Presets are only shortcuts that fill in the target. |
| Presets carry every hidden setting | **Partly** | Hidden settings come from the hardware baseline and the governor's learned profile, not from fixed preset tables. |
| Connection test (rounds of 16 to 256 parallel downloads, 30 s) | **Yes** | Used as the governor's warm start on first run and as a feasibility check in the wizard. |
| VRAM detection from the driver registry | **Yes**, extended | Windows registry as OSXP, plus Linux/macOS sources. Feeds the guards. |
| Graphics memory measured per texture size from flights | **Yes** | Feeds the VRAM guard and the target cost preview. |
| Paging model with `k` measured in flight | **Already ported** (`paging_model.py`) | The governor stores the fitted `k` so each flight starts from it. |
| Post-flight tuner: one step per flight, opt-in | **Changed** | Becomes the slow loop of the governor (cross-flight learning). Live tuning handles the rest. Opt-in behaviour remains as *Advise* mode. |
| Per-preset tuned store, Reset | **Yes**, generalised | Learned profiles are stored per (target profile, imagery provider, network), with Reset. |
| Cap texture size at 4096 px and shrink finer imagery | **Optional lever** | Off by default because it changes what the user asked for. If the user allows it, the governor can use it to fit VRAM limits without lowering the zoom level. |
| No altitude settings | **No** | Dynamic zoom steps stay. They now also get a cost preview and VRAM checks. |
| Settings read-only while streaming | **No** | Targets and limits can change while flying; the governor adapts. Only mount-level settings (paths, FUSE) require unmounting. |
| Flight report with a verdict | **Yes**, extended | Adds target fidelity per flight phase and what the governor learned. |

---

## 3. AutoOrtho today: the problems this solves

- **148 keys, about 89 controls, 9 categories.** There are two waiting systems side by side (`maxwait` / `tile_time_budget` / `fallback_*` and the `[streaming]` wait bands), 13 provider settings plus 3 legacy names, and about 12 worker and memory sizes.
- **Engine settings are frozen at startup.** For example: `live_builder_concurrency` and `background_builder_workers` size thread pools once (`getortho.py:8740`), `live_tile_admission` creates a fixed `BoundedSemaphore` (`getortho.py:1418`), and prefetch radius and lookahead are read in the prefetcher constructor (`getortho.py:5344`). Only the broker's per-origin AIMD controller adapts at runtime.
- **No real resource checks.** RAM and VRAM appear only in tooltip text ("every extra ZL increases VRAM … by 4x"). Nothing compares a chosen target with the machine. Because the texture size is `256 × chunks` (`_calc_required_buffer_size()` switches to 8K at ZL17 bulk or ZL19 airports), one slider step can quietly quadruple graphics memory.
- **Profiles are fixed tables.** The four "performance profiles" (`ui/main_window.py:_apply_settings_preset`) write UI widgets with values that suit nobody's PC in particular. For example, "Quality" sets `tile_time_budget` to 300 s.
- **The wizard does not ask what the user wants and measures nothing.**
- **Settings are defined in four or five places:** the `_defaults` string, two widget-name dictionaries, `ui/config_validation.py`, and the slider ranges.

---

## 4. Concepts in detail

### 4.1 Target: what the user wants

The existing keys stay, with the same meaning, so current configs keep working:

- `max_zoom_mode = fixed | dynamic`
- `max_zoom`, `max_zoom_near_airports` (fixed mode)
- `dynamic_zoom_steps` (altitude → zoom and airport zoom)

What changes is how they are presented and checked:

- **Quality target page** with two modes:
  - *Fixed:* two sliders (Scenery ZL, Airport ZL).
  - *By altitude:* the existing step editor and chart (`ui/dynamic_zoom_editor.py`), with its Airliner / General Aviation / Low VRAM presets.
- **Live cost preview** under the target, recomputed on every change:
  - Graphics memory for photo textures near the aircraft, against this PC's card (from the paging model, the texture size per step and measured flight data).
  - Download per new 1° area, and the Mbit/s needed to keep up at a typical speed for that altitude step.
  - Disk per 1° area.
  - A traffic light for each, from the guards (§4.3).
- **Target presets are shortcuts, not modes.** Picking "Airliner" fills the steps; the user can then edit them. A short "Not sure? Let AutoOrtho suggest a target" button uses the connection test plus detected hardware to fill in a target that should be reachable.
- `min_zoom` moves to Expert. The low-zoom base already covers far scenery.

**Target fidelity** is the governor's goal, and the number shown to the user:

> *Target fidelity* = the share of X-Plane's near-band reads (mips 0 to 1) answered at the target zoom within the wait budget, over the last N minutes.

Plus *time to sharp*: after a new area loads, how long until near reads are at the target.

### 4.2 Limits: what the user allows

New `[limits]` section. Every value defaults to `auto` (derived from the detected machine). The user can set a lower ceiling:

| Limit | `auto` means | Governs |
|---|---|---|
| `ram_gb` | AutoOrtho may use up to about 25 % of physical RAM, and always leaves max(4 GB, 20 %) free for X-Plane and Windows | memory cache, predictive memory, tile image cache, write buffers, build buffers |
| `vram_gb` | card memory minus an X-Plane reserve (about 5 GB, adjustable) | the target's projected photo-texture memory; texture-size lever if allowed |
| `bandwidth_mbps` | unlimited (the governor finds the line's capacity) | download concurrency ceiling, plus an optional token bucket so other apps keep some bandwidth |
| `cpu_cores` | physical cores minus 2, kept for X-Plane | builder workers, native pipeline threads, background builds |
| `disk_cache_gb` | today's `file_cache_size` | disk budget manager |
| `protect_sim_fps` *(optional)* | off | if on, the governor backs off background work when X-Plane's frame time rises while AutoOrtho is busy (needs the `sim/operation/misc/frame_rate_period` dataref added to `datareftrack.py`) |

Limits are **hard ceilings for the governor**. The governor never exceeds them, even if fidelity suffers. When a limit is the reason the target isn't reached, the UI says so ("Bandwidth limit 50 Mbit/s reached; target fidelity 81 %").

### 4.3 Guards: protection that warns but does not block

Today AutoOrtho has no RAM or VRAM checks beyond tooltip text, so these are new. They run in three places:

1. **When editing the target or limits.** The cost preview turns amber or red, with a one-line reason and a **Fix** button. Fix offers the nearest target that fits (for example "lower airport ZL to 18"), or raises the relevant limit if headroom exists.
   - VRAM: projected photo-texture memory for the highest step + X-Plane reserve > card → red.
   - RAM: limits set above what the machine can spare → red.
   - Disk: cache folder free space < cache limit + safety margin → amber.
   - Bandwidth: measured capacity (connection test or learned profile) < what the target needs at that step's typical speed → amber ("expect blurry moments in new areas").
2. **At mount time.** The same checks run once more and show a notice on Home if something changed (another drive, new card, very low free RAM).
3. **During the flight (emergency brakes).** These act immediately, outside the governor's normal step rules:
   - Free RAM below max(1.5 GB, 8 %) → pause prefetch, shrink caches to their minimum, drop background builds. Release gradually once RAM recovers.
   - AutoOrtho's own memory above `ram_gb` → same.
   - Disk free below the safety margin → stop writing new cache entries, keep serving.
   - Each activation shows on the live panel and in the log, and is counted in the flight report.

### 4.4 Governor: how AutoOrtho gets there

The governor runs in the parent process. It sees every mount worker's stats (through the existing `aostats` manager store), the broker, and system counters. It adjusts *levers* within the ranges allowed by the limits.

#### Control loops

| Loop | Period | Mechanism | Owner |
|---|---|---|---|
| Fast | sub-second | per-origin AIMD in the broker, priority queues, wait bands | existing code, unchanged |
| **Medium** | 5 to 10 s | **bottleneck controller:** find what is holding fidelity back, move one lever one step, check the result, keep or revert | **new** |
| Slow | per flight and across flights | learned profile: stores where the medium loop settled, the fitted paging `k`, measured graphics memory; warm-starts the next flight; tells the user when the target is consistently out of reach | new |

The medium loop sets the *ceilings* the fast loop works inside. For example, the governor raises the broker's per-origin maximum, and AIMD still ramps up to it response by response. The two loops do not fight.

#### Signals (sampled every 2 s, smoothed)

- **Fidelity and waits:** near and middle reads served at target vs below target, stand-ins, wait-band timeouts, `chunk_missing_count`, `upscaled_chunk_count`, `row_batch_deadline_expired`.
- **Network:** throughput, tile latency (p50/p90), error and timeout share per origin, broker queue depth and time spent queued.
- **Compute:** builder utilisation, `streaming_builder_queue_wait_count`, tile-queue waits, AutoOrtho CPU %, system CPU %.
- **Memory:** free RAM, AutoOrtho RSS (parent + workers), cache hit rates (`tile_mem_hits` / `tile_mem_miss`).
- **Flight context:** phase (starting / taxi / climb / cruise / approach) from `wait_bands.StartTracker`, altitude, ground speed, current dynamic zoom step.
- **Optional:** X-Plane frame time (see `protect_sim_fps`).

#### Bottleneck classifier (rule table, evaluated each tick)

| State | Signs | Lever moved (one step) |
|---|---|---|
| **Network-bound, line has room** | broker queue full, no errors, tile latency stable, throughput under the measured line capacity and under `bandwidth_mbps` | raise download concurrency ceiling |
| **Network-bound, line full / server pushing back** | errors or timeouts > 3 %, or latency rising with no throughput gain | lower concurrency ceiling; reduce prefetch depth so live reads get the line |
| **Compute-bound** | chunks waiting in builder queues, builders 100 % busy, CPU under `cpu_cores` | raise live builders (then background builders) |
| **Compute-bound at limit** | same, but CPU at limit, or sim frame time rising | lower background builds, raise build interval; keep live builders |
| **Memory pressure** | free RAM falling toward the guard, or RSS near `ram_gb` | shrink tile image cache, then predictive memory, then memory cache |
| **Wait-bound** | near reads time out, but the real chunk arrives soon after (p80 ≤ 12 s) | raise `band_max_wait_seconds` (or `near_wait_seconds` while starting) |
| **Behind the aircraft** | near reads hit tiles that prefetch never queued, line and CPU have room | extend prefetch lookahead and radius |
| **Headroom everywhere** | fidelity ≥ 98 % and resources well under limits | slowly decay levers toward the baseline, to free resources for X-Plane |
| **Target unreachable** | every lever at its limit for N minutes, fidelity still low | no lever left: degrade gracefully (below) and notify |

#### Rules that keep it stable

- One lever per tick. Hysteresis bands. A cooldown per lever (for example 30 s).
- **Keep or revert:** after a change, compare fidelity and the bottleneck signal over the next window. If either is worse, revert and put that lever on a longer cooldown. This is a simple hill-climb with rollback, so the governor can't drift into a bad state.
- Phase-aware step sizes: big steps while the flight is starting (getting to sharp scenery quickly matters most), small steps in cruise.
- **Pinned values are never touched.** Hard limits are never exceeded.

#### Graceful degradation when the target is out of reach

Applied in this order, each step shown on the live panel:

1. Spend less on far work: shorter prefetch lookahead, smaller radius.
2. Far and middle bands may use a zoom one level below target (mostly already true via the low-zoom base and wait bands; the governor makes it explicit and measured).
3. **Optional, if the user allowed it:** cap texture size (shrink imagery above 4096 px instead of building 8K). Same zoom level, a quarter of the VRAM.
4. Notify the user: "Your ZL18 target reached 72 % fidelity in cruise. The limit is your connection (about 60 Mbit/s). Options: lower the cruise step to ZL16 · raise the bandwidth limit · keep it".

The governor **never silently lowers the user's target**. Only the user changes the target.

#### Learning across flights (the slow loop)

- At unmount, save the converged lever values, the measured line capacity, the fitted paging `k` and the measured graphics memory to `~/.autoortho-data/profile/learned.json`, keyed by (target profile hash, imagery provider, network fingerprint such as gateway MAC or SSID hash).
- The next flight with the same key **starts from those values**, so it is near-optimal from the first minute.
- First ever flight: warm start from the connection test (concurrency) and the hardware baseline (workers and memory).
- **Reset learned settings** on the Tuning page clears the profile.

#### Modes

| Mode | Behaviour |
|---|---|
| **Auto** *(default)* | Adjusts live, within limits, shows everything. |
| **Advise** | Computes the same decisions but only lists them on Home (OSXP style). Nothing changes without Apply. |
| **Off** | Fixed values from the registry and user settings, as today. For A/B testing and troubleshooting. |

---

## 5. Transparency

The governor must be easy to inspect. Users should be able to see what it is doing without reading the log.

### 5.1 Home: live tuning card (while mounted)

```
Quality target   ZL17 · airports ZL18 (By altitude: "General Aviation", step 0-5000 ft)
Delivering       96 % at target · sharp in 38 s after the last area load
Bottleneck       Network: 142 of ~160 Mbit/s       [Limits: RAM 6.1/8 GB · CPU 6/10 cores · VRAM ok]
Last change      +16 parallel downloads: queue was full, no errors (12 s ago)       [Details]
```

### 5.2 Tuning page

- **Table:** setting · current value · allowed range (from limits) · source (*baseline / learned / governor / pinned*) · last change and reason · pin toggle.
- **Timeline chart** for the session: fidelity, throughput, CPU, RAM, with each governor decision marked. Click a marker to see the decision and what happened after.
- Buttons: **Mode** (Auto / Advise / Off) · **Reset learned settings** · **Export tuning report**.

### 5.3 Log and report

- Every decision is logged on one line: `Governor: raise provider ceiling 128→144 (network-bound: queue 100 %, errors 0.2 %, 142 Mbit/s); fidelity 91 %→?`, followed by a `kept` or `reverted` line.
- The flight report (existing `diagnostics` Markdown/JSON) gains:
  - a verdict line,
  - fidelity by phase,
  - guard activations,
  - what was learned for next time ("starts next flight at 144 parallel downloads, 10 live builders").

---

## 6. Settings structure

### 6.1 One registry

Replace the `_defaults` string, the widget-name dictionaries and the scattered ranges with one registry (`autoortho/settings/registry.py`):

```python
@dataclass(frozen=True)
class SettingSpec:
    key: str                 # "autoortho.live_builder_concurrency"
    type: type
    default: Any
    range: tuple | None      # absolute bounds; limits may narrow them
    tier: Tier               # ESSENTIAL | TUNING | EXPERT | INTERNAL | RETIRED
    owner: Owner             # TARGET | LIMIT | USER | GOVERNED | HARDWARE | FIXED
    live: bool               # can change without remount
    lever: LeverSpec | None  # for GOVERNED: step size, cooldown, which limit bounds it
    label: str
    help: str
    replaced_by: str | None
```

### 6.2 Layered resolution

From lowest to highest priority:

```
packaged default
  → hardware baseline     (cores, RAM, VRAM: computed at start)
  → learned profile       (from previous flights with the same target/provider/network)
  → governor live value   (in memory only, during the flight)
  → user pin              (explicit value: the governor never touches it)
clamped by limits         (hard ceiling for everything above, except pins, which the guards warn about)
= effective value + source + reason
```

- **The ini stores intent only:** paths, target, limits, mode, look settings and pins. Values equal to their computed value are not written. A fresh file is about 25 lines instead of about 150.
- **Startup consumers do not change.** The resolver runs inside `AOConfig.load()` and fills the same `CFG.<section>.<key>` attributes read today.
- **Live levers** need the engine changes in §8.

### 6.3 UI tiers

| Tier | Contents |
|---|---|
| **Essentials** (about 15 controls) | Quality target (Fixed / By altitude + presets + cost preview) · Limits (Auto, or custom sliders) · Governor mode · X-Plane / Scenery / Cache folders · Cache limit · Imagery source · Seasons on/off + strength · Night exclusion · Reduce sunrise shine · SimBrief ID + use flight plan · Hide window while running |
| **Tuning** (live) | The table, timeline and buttons from §5.2 |
| **Expert** | A searchable table of every registry key: effective value, default, source and help. Editing a value pins it. Home shows "3 pinned expert values · Review". The current category pages (DDS, pipeline, FUSE, provider, diagnostics) can stay as grouped views over the same registry during the transition. |

### 6.4 Apply model

Keep Apply/Revert. Targets, limits and mode are `live=True`: Apply takes effect immediately and the governor adapts. Mount-level keys (paths, FUSE, pipeline mode) show "Takes effect at next mount" and are disabled while mounted.

---

## 7. First-run wizard (six steps)

1. **Welcome and checks:** FUSE / WinFsp / macFUSE readiness (merges today's Dependencies page).
2. **X-Plane:** auto-detect first, browse second.
3. **Scenery:** install a region or point at existing packages.
4. **Cache:** one folder, free space, a suggested cache limit. Long-term cache and download directory move to Expert.
5. **Quality target:**
   - Pick Fixed or By altitude, with presets.
   - **Test my connection** (30 s, OSXP's method through AutoOrtho's broker). The cost preview turns green, amber or red for this PC.
   - "Suggest a target for me" fills in a target that should be reachable.
   - The test result also seeds the governor.
6. **Limits and ready:** "Use what's available (recommended)" or set limits. Summary, plus "Start AutoOrtho now".

---

## 8. Engine work: making levers live

Most levers are read once today. Each needs a small, mechanical change so the governor can move it.

| Lever | Read today | Change |
|---|---|---|
| Download concurrency ceiling | broker `_adaptive_config` at start | broker accepts `set_origin_ceiling()` from the parent (same process; easy) |
| Bandwidth cap | none | token bucket in the broker dispatcher |
| `live_builder_concurrency`, `background_builder_workers` | pool sizes at `getortho.py:8740` | create pools at the ceiling (the `cpu_cores` limit); gate them with a **resizable semaphore** whose permit count the governor sets |
| `live_tile_admission` | `BoundedSemaphore` at `getortho.py:1418` | same resizable gate |
| `prefetch_lookahead`, `prefetch_radius_nm`, `prefetch_max_chunks` | prefetcher constructor (`getortho.py:5344`) | read through a `LiveSettings` accessor each cycle |
| `predictive_dds_memory_mb`, `tile_image_cache_mb`, `cache_mem_limit` | budgets at init | budget objects with `resize()`; shrinking evicts down to the new size |
| `band_max_wait_seconds`, `near_wait_seconds`, `paging_k` | `streaming_options` (already read at call time) | none |
| Texture size cap (optional) | none | `TexturePlan` + downscale before encode + plan-keyed DDS cache (the largest item; can come last) |

**Parent → worker channel.** Mount workers are separate processes (`process_supervisor.py`). Add a small `governor/` key space to the existing `aostats` manager store (already shared and authenticated). The governor writes lever values there; each worker's `LiveSettings` polls it every 1 to 2 s. No new IPC mechanism is needed.

**Wait consolidation.** Before the governor can tune waits cleanly, the wait bands should be the only live-read policy. List every caller of `_row_wait_seconds()` / `QualityDeadlinePolicy` that still uses `tile_time_budget` / `maxwait`, move them to band budgets, then retire the legacy keys.

---

## 9. Setting classification (summary)

Owner: **T** target · **L** limit · **U** user · **G** governed lever · **H** hardware baseline · **M** measured/learned · **F** fixed default · **R** retire.

| Group | Keys | Owner | Tier |
|---|---|---|---|
| Target | `max_zoom_mode`, `max_zoom`, `max_zoom_near_airports`, `dynamic_zoom_steps` | T | Essentials |
| Limits *(new)* | `limits.ram_gb`, `vram_gb`, `bandwidth_mbps`, `cpu_cores`, `protect_sim_fps`; `cache.file_cache_size` → `disk_cache_gb` | L | Essentials |
| Governor *(new)* | `governor.mode`, `allow_texture_cap`, `xplane_vram_reserve_gb` | U | Essentials / Expert |
| Paths | `xplane_path`, `scenery_path`, `cache_dir` | U | Essentials |
| | `long_term_cache_dir`, `download_dir`, `dsf_dir`, `log_file` | U | Expert |
| Look | `maptype_override`, `seasons.enabled`, `terrain.default_roughness`, `time_exclusion.enabled`, `simbrief.userid`, `simbrief.use_flight_data`, `general.hide` | U | Essentials |
| Governed: network | `provider_max_in_flight`, `provider_origin_initial_concurrency`, `provider_origin_max_concurrency` | G (+M warm start) | Tuning |
| Governed: compute | `live_builder_concurrency`, `background_builder_workers`, `live_tile_admission`, `buffer_pool_size`, `streaming_builder_pool_size`, `predictive_dds_build_interval_ms` | G (bounded by `cpu_cores`) | Tuning |
| Governed: memory | `cache_mem_limit`, `predictive_dds_memory_mb`, `tile_image_cache_mb`, `cache_write_buffer_mb` | G (bounded by `ram_gb`) | Tuning |
| Governed: prefetch | `prefetch_lookahead`, `prefetch_radius_nm`, `prefetch_max_chunks` | G | Tuning |
| Governed: waits | `near_wait_seconds`, `band_max_wait_seconds` | G | Tuning |
| Measured | `paging_k`, graphics memory per size, line capacity | M | Tuning (read-only) |
| Engine constants | AIMD constants (`provider_origin_min/increase/threshold/decrease/cooldown`), `download_dispatch_workers`, `provider_queue_timeout`, `prefetch_interval`, `prefetch_admission_burst`, `tile_queue_max_size`, `native_pipeline_threads`, `pipeline_mode`, `[pydds]`, `[fuse]`, `[flightdata]`, `[diagnostics]`, DDS cache keys, `[streaming]` A/B switches, `fallback_*`, `prefetch_quality_*`, `min_zoom`, `missing_color`, `simheaven_compat`, `using_custom_tiles`, season saturations, remaining SimBrief / night keys | F | Expert |
| Retire now | `live_aopipeline_min_chunk_ratio`, `ephemeral_dds_cache_mb`, `max_concurrent_downloads`, `http2_max_connections`, `prefetch_pipeline_v2` (after one release); `fetch_threads` only for HTTP/1.1 mode | R | — |
| Retire after wait consolidation | `maxwait`, `suspend_maxwait`, `use_time_budget`, `tile_time_budget` | R | — |

---

## 10. Migration

- Bump `CONFIG_REVISION` and extend `_migrate_config_revision()`.
- **Target keys are kept as they are.** Nobody's scenery changes after the update.
- Engine keys equal to the old packaged defaults are dropped and become governed.
- **Engine keys that differ become pins.** The governor respects them. Home shows "AutoOrtho now tunes engine settings automatically. 4 of your values were kept as pinned · Review", with **Unpin all**.
- Old retired keys are migrated (`_migrate_legacy_provider_options` exists) and removed.
- The four performance profiles are removed. Their intent is now covered by the target presets, the limits and the governor.

---

## 11. Rollout (each phase can ship on its own)

| Phase | Content | Risk | User benefit |
|---|---|---|---|
| **1. Registry + resolver** | Registry generated from `_defaults`; resolver fills `CFG`; sparse writes; retire R keys; Expert table | Low: consumers unchanged | Smaller config, visible overrides |
| **2. Hardware baseline + guards** | Detect cores/RAM/VRAM/disk; derive starting values; cost preview and guard checks on the target page; emergency RAM brake | Low | Protection from over-sized targets |
| **3. Limits + connection test** | `[limits]`; port the connection test; wizard Quality and Limits steps | Low | Fast setup, safe ceilings |
| **4. Live levers** | Resizable gates and budgets, `LiveSettings`, parent → worker channel | Medium: touches pool creation | Prerequisite for the governor |
| **5. Governor in Advise mode** | Signals, classifier, decisions shown but not applied; live panel and timeline | Low: changes nothing | Validates decisions on real flights |
| **6. Governor in Auto mode** | Applies decisions with keep/revert; learned profiles and warm start | Medium | Settings tune themselves |
| **7. Wait consolidation** | Bands become the only live policy; retire legacy wait keys | Medium | Removes the most confusing group |
| **8. Optional texture-size lever** | `TexturePlan`, downscale before encode, plan-keyed DDS cache | Medium–high: native pipeline | Higher zoom within VRAM limits |
| **9. Home revamp (§13)** | State-based layout, `HomeSnapshot` from the sampler, notices, last-flight card. Can start after phase 2 with stats only; the fidelity hero and Auto-tuning block arrive with phases 5–6 | Low: UI only | Users see what AutoOrtho is doing and whether it keeps up |

Shipping Advise before Auto lets us compare the governor's decisions with what experienced users would do, using real flight reports, before it changes anything.

---

## 12. Risks and open questions

- **Oscillation and noise.** Mitigated by one lever per tick, hysteresis, cooldowns, keep/revert, and smoothing over windows. Needs a **replay harness**: record signals from real flights (the diagnostics checkpoints already capture much of this) and run the governor offline against them in tests.
- **Interaction with AIMD.** The governor only moves ceilings; AIMD keeps the per-response loop. Check that a lower ceiling never undercuts AIMD's floor (`provider_origin_min_concurrency`).
- **Latency of the cross-process channel.** A 1 to 2 s poll is fine for the medium loop. Emergency brakes should run in each worker locally as well, not only in the parent.
- **Pinned values that break the target.** The guards warn ("pinned live_builder_concurrency=2 is limiting fidelity"), but pins are respected.
- **VRAM accuracy on macOS and Linux.** Unknown VRAM → no VRAM warnings and a conservative suggested target.
- **Network fingerprint privacy.** Store only a salted hash, locally.
- **Frame-time protection** needs a new dataref subscription. Verify how fast `datareftrack` updates and how noisy the value is before relying on it.
- **Restart semantics:** confirm per key what a running mount can pick up live, and set `SettingSpec.live` accordingly.

---

## 13. Home (main menu) revamp

The Target / Limits / Governor model needs a front page that shows it working. Today's Home can't do that.

### 13.1 What Home shows today

`ui/pages/home_page.py` shows **ten cards of equal weight** in a 2-column grid: Runtime, Readiness, X-Plane, Mounted Scenery, Provider, SimBrief, Cache, Task, Throughput, Recent Failure.

- **Nothing about streaming.** *Task* and *Throughput* are filled from the scenery-package download task manager (`_refresh_phase3_task_views`), not from the streaming engine. While flying, Home shows no imagery speed, no quality, and no resource use.
- **Everything has the same weight.** "SimBrief: No flight plan" takes as much space as "Runtime".
- **Cards stay up when they have nothing to say.** For example "✓ No recent failures" and "○ Idle".
- **No sense of time.** There are no trends, so you can't tell "slow right now" from "slow all flight".
- The raw counters exist (`aostats`, the web `templates/stats.html` page), but they are engine counters (chunks, hits, queue skips), not answers.

### 13.2 Principles

1. **Answer four questions, in this order:**
   1. Is it working?
   2. Am I getting the quality I asked for?
   3. What is it using?
   4. Do I need to do anything?
2. **Every number must be actionable or reassuring.** If a value would not change what the user understands or decides, it belongs on the Tuning or Diagnostics page, not on Home.
3. **The layout follows the state.** Stopped, Starting, Flying and After flight each show what matters then. Cards don't sit around showing nothing useful.
4. **One headline, detail on demand.** Each block has one number. Clicking it opens the detail (Tuning page, timeline, diagnostics report).
5. **Plain units.** % sharp, Mbit/s, GB, minutes and nm. Never chunks, mipmaps, permits or queue depths.
6. **Calm updates.** Numbers refresh at 1 Hz with smoothing; trend lines use 5 s buckets. Values freeze, labelled "paused", while X-Plane is paused. Nothing flickers.

### 13.3 Layout while flying

```
┌──────────────────────────────────────────────────────────────────────────────────────┐
│ ● Flying with AutoOrtho   4 scenery packs · X-Plane connected        [ Unmount ]   │  status strip
│   EDDM → LOWI · climbing 8,400 ft · 260 kt · Bing                                    │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ ⚠ Your ZL18 airport target reached 74 % at LOWI. Limit: connection (~60 Mbit/s).     │  notices (0–3)
│   [Lower airport ZL to 17]  [Raise bandwidth limit]  [Keep]                          │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ SCENERY QUALITY                                                                       │  hero
│   96 % sharp at target        Target ZL17 · airports ZL18 · step "5,000–12,000 ft"   │
│   ▁▂▅▇█████▇██████▆███  (session; red = blurry moments)   Sharp 38 s after last load │
├────────────────────────────┬────────────────────────────┬────────────────────────────┤
│ NETWORK          ◆ limit   │ COMPUTER                   │ GRAPHICS MEMORY            │  resource gauges
│ 142 Mbit/s of ~160         │ CPU 6 of 10 cores          │ ~3.1 GB photo textures     │
│ ▃▅▇▇▆▇█▇  0.3 % retries    │ RAM 6.1 of 8 GB (AO)       │ of 7 GB budget (12 GB card)│
├────────────────────────────┴────────────────────────────┴────────────────────────────┤
│ ACTIVITY   18,420 textures served · 71 % from cache · 3.8 GB downloaded              │
│            Building 9 now · Ahead of you: next ~6 min / 26 nm ready                  │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ AUTO-TUNING (Auto)   +16 parallel downloads: queue full, no errors · 12 s ago         │
│                      7 changes this flight · 2 pinned values           [See tuning]  │
└──────────────────────────────────────────────────────────────────────────────────────┘
  Open map · Scenery library · Diagnostics
```

| Block | What it answers | Source |
|---|---|---|
| **Status strip** | Working? One state pill, the main action, X-Plane link, aircraft phase, altitude, ground speed, provider. It replaces the Runtime, X-Plane, Mounted Scenery and Provider cards. | runtime state, `datareftrack`, `wait_bands.StartTracker` |
| **Notices** | Do I need to act? At most 3, sorted by severity, each with buttons, dismissable. Sources: target unreachable, guard activations, readiness problems, update available, Advise-mode suggestions, worker crash/restart. It replaces the Recent Failure card and only appears when there is something to say. | governor, guards, readiness, crash handler |
| **Scenery quality (hero)** | Am I getting what I asked for? Fidelity % (last 5 min), the active target and dynamic-zoom step, time to sharp, and a session trend with blurry moments in red. Clicking it opens the timeline. | governor fidelity metric (§4.1) |
| **Resource gauges** | What is it using? Three compact gauges: usage against limit or capacity, with the colour showing headroom. The gauge the governor considers the bottleneck gets a ◆ chip. RAM shows AutoOrtho's own use against `ram_gb`; a second line appears only when *system* free RAM is low. | broker throughput and retries, psutil, VRAM estimate |
| **Activity** | Is it ahead of me? Textures served, cache share, data downloaded, builds in progress, and **prefetch horizon** (how far along the predicted path is already sharp, in minutes and nm). | `aostats` counters, prefetcher, paging model |
| **Auto-tuning** | What changed and why? Mode, last decision with its reason, number of changes, number of pins. | governor decision log |

### 13.4 The other states

| State | Home shows |
|---|---|
| **Stopped (ready)** | Status strip with **Mount**. A one-line *Ready to fly ✓* that expands into the readiness checklist only when something is wrong. **Quality target** summary with the cost-preview traffic lights (§4.1) and an *Edit* link. **Last flight** card (below). **Storage**: cache used / limit and disk free, from the existing storage scan. Installed scenery regions, with an update badge. |
| **Starting / X-Plane loading** | A progress block that replaces the hero: *Mounting 4 packs ✓ · Waiting for X-Plane ✓ · Loading area: 63 % of nearby scenery sharp (about 40 s left)*. Users currently stare at X-Plane's loading screen with no idea whether AutoOrtho is working; near-band reads during `StartTracker.starting` give this number directly. |
| **Flying** | §13.3. |
| **Paused / in menu** | Same layout, dimmed. The pill reads *Paused*, and the counters stop. |
| **After flight** (until the next mount or dismiss) | **Last flight** card expanded. Verdict (*Smooth flight* / *A few blurry moments* / *Often blurry*), average fidelity by phase (departure, cruise, arrival), distance and duration, data downloaded and cache share, guard activations, and *Learned for next time* (the warm-start values). Buttons: *Open report*, *Open map with blurry spots*. |

### 13.5 What stays off Home

These remain on the Tuning page, Diagnostics, or the web stats page. They are useful when troubleshooting but are noise for everyone else:

- chunk counts, hit/miss counters, submit-skip reasons, partial-build and healing counts
- mip levels, per-band read counts, paging `k`
- broker queue depth, per-origin AIMD limits, connection counts
- buffer pool, builder queue lengths, per-stage latency histograms
- per-process memory and PIDs, FUSE thread counts
- scenery-package install task progress (moves to a small badge on *Scenery library* plus a notice when finished or failed)

If one of these explains a problem, it reaches Home **as a notice in plain words**, not as a raw number. For example: "Imagery server is refusing requests (12 % retries); downloads were slowed down automatically".

### 13.6 Optional extras

- **Tray / taskbar summary:** when the window is hidden (`general.hide`), the tray tooltip reads `AutoOrtho · 96 % sharp · 142 Mbit/s · Flying`, and the icon turns amber or red with a notice. It is cheap and useful for users who keep the window hidden.
- **Tile radar:** a small QPainter widget, no web view. It shows tiles around the aircraft coloured by state (sharp at target / building / below target / not loaded), plus the prefetch corridor. It shows at a glance whether AutoOrtho is ahead of the aircraft. Clicking it opens the full map.
- **Compact window mode:** status strip + hero + gauges only, for a second monitor.

### 13.7 Data plumbing

- **The UI does not poll the stats store.** The governor's sampler (§4.4) already collects every signal each 2 s. It publishes a `HomeSnapshot` dataclass at 1 Hz with smoothed values and the session trend buffers. `HomePage` gets it through one Qt signal and only formats it.
- **New metrics needed:**
  - fidelity and time to sharp (governor)
  - loading progress at start (near-band reads during `starting`)
  - prefetch horizon (prefetcher + predicted path)
  - photo-texture VRAM estimate (as `GraphicsMemoryMeter` in OSXP)

  Everything else exists today.
- **Governor in Off mode:** the sampler still runs, so Home shows the same stats without the Auto-tuning block.
- **Accessibility:** every gauge and colour has a text equivalent (`setAccessibleName` with the full sentence), state is never shown by colour alone, and keyboard focus order follows the reading order above.

---

## Appendix A: OSXP source map (for the ported pieces)

| Topic | File (under `OrthoStreamXP/src/OrthoStreamXP/`) | Used for |
|---|---|---|
| Connection test | `Setup/Settings/ConnectionTest.cs` | Wizard test, governor warm start |
| VRAM detection, per-size memory | `Tuning/GraphicsMemory.cs` | Guards, cost preview |
| Preset advisor | `Tuning/PresetAdvisor.cs` | "Suggest a target for me" |
| Post-flight rules | `Tuning/Tuner.cs` | Seeds the classifier's rule table and thresholds |
| Flight measurements | `Tuning/FlightFacts.cs` | Signal list, RAM-low threshold |
| Tuned store, Reset | `Tuning/TunedSettings.cs` | Learned profile store |
| Texture size cap | `Textures/TextureQuality.cs` | Optional lever (phase 8) |
| Paging model | `Scenery/Paging/PagingModel.cs` | Already ported as `autoortho/paging_model.py` |
