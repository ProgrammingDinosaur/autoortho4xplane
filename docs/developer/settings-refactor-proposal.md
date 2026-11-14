# Settings refactor proposal: Target, Limits, Governor

Status: phases 1 to 9 implemented (see "Implementation notes" at the end); the governor ships in Auto mode, starting from the tuning plan (see "Tuner completion" at the end)
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

---

## Implementation notes

### Phase 1 (registry, sparse file, Expert table)

- The registry is `autoortho/settings_registry.py`, built from the existing `_defaults` template, so there is still one place to change a default.
- `AOConfig.is_custom()` / `source()` / `reset_to_default()` / `set_value()`; the file stores only custom settings as active lines (`CONFIG_REVISION = 2`).
- The Expert table is `autoortho/ui/pages/expert_settings_page.py`; settings with a control elsewhere are read-only there.

### Phase 2 (hardware baseline, guards, RAM brake)

- `autoortho/hardware.py`: cores and RAM (psutil) and graphics memory (Windows registry as in OSXP, `nvidia-smi` / sysfs on Linux, unified memory on Apple silicon). `baseline_values()` holds the scaling rules.
- Resolution layer: `AOConfig._apply_hardware_baseline()` sets non-custom keys to the baseline; `automatic_text()` is what reset gives; `source()` returns `"hardware"`. Baseline values are not written to the file. `[tuning] hardware_baseline` turns it off (fixed at load, so turning it off never pins the scaled values).
- `autoortho/target_cost.py`: the cost model. Per flight phase it flies a straight 20-minute path and sums, per texture, the detail wanted at closest approach from `paging_model.mip_for_k` (graphics memory, imagery bytes at 16 KB a chunk, download rate). Departure phases add the ZL18 airport textures wanted on the ground. About 2 ms per target.
- `autoortho/guards.py`: VRAM / RAM / disk / download checks and `suggest_target()`, a greedy one-level-at-a-time search for the nearest target within 90 % of the card. The disk guard uses free space only; the bandwidth guard waits for the phase 3 connection test.
- UI: `autoortho/ui/widgets/target_cost_panel.py` under the zoom controls, fed from the controls before Apply; a "PC resources" readiness check (graphics memory and RAM only) shows on Home and in Diagnostics and is refreshed after each save.
- `autoortho/ram_brake.py`: each worker samples free RAM at most once a second; engaged, it skips prefetch cycles, holds background builds and halves the memory cache limit in the tile clean loop. Counted as `ram_brake_activations`.
- Not done in phase 2: the mount-time notice uses the readiness check rather than a separate Home banner (phase 9 revisits Home).

### Phase 3 (limits, connection test, wizard steps)

- `autoortho/limits.py`: `[limits]` `ram_gb`, `vram_gb`, `bandwidth_mbps`, `cpu_cores`, each `auto` or a number (registry kind `auto`). `resolve()` gives the effective limits; `clamp_automatic()` lowers the automatic values of `cache_mem_limit`, `predictive_dds_memory_mb` and the two builder counts. Auto limits never lower the hardware baseline (tested). `protect_sim_fps` is left for the governor phases, and `disk_cache_gb` stays `cache.file_cache_size`.
- Resolution order in `AOConfig._apply_hardware_baseline()`: packaged default, hardware baseline, connection-test warm start (`provider_origin_initial_concurrency`), limits; user values are never clamped. `source()` adds `"limit"` and `"measured"`.
- Engine: the RAM brake also engages when AutoOrtho's aggregated worker memory exceeds `ram_gb` minus the app's share; `AO_CPU_THREAD_LIMIT` (set by the parent) sizes `_native_cpu_budget()` and each worker's OpenMP variables (`process_supervisor.apply_openmp_budget`); the broker gets `max_bytes_per_second` and throttles body reads with a token bucket (`_ByteRateLimiter`).
- `autoortho/connection_test.py`: OSXP's method (rounds of 16 to 256, steady middle 10-90 %, fewest at once reaching 90 % with under 1 % failed) over an httpx HTTP/2 client like the broker's. Provider URLs moved to `autoortho/provider_urls.py`, shared with `getortho.Chunk` (identical URLs, checked against the old templates). A live run against Bing took 3.3 s: 90 Mbit/s, 64 at once, 15-19 KB a chunk.
- Guards: the graphics memory budget is the custom `vram_gb` when set; the download check compares the need with the connection test and the bandwidth limit; the RAM check warns above the RAM limit; `suggest_target_for_pc()` picks the sharpest fixed target both allow.
- UI: Settings > Limits (`LimitsEditor`, `ConnectionTestPanel`), "Suggest a target for this PC" in the cost panel, and two wizard steps (Quality target, Limits) before Review.

### Phases 1-3 completed (Essentials and the remaining gaps)

- Essentials tier (§6.3): `ui/pages/essentials_page.py`, the first settings category. Its controls mirror the full controls (`ui/widgets/mirror.py`), so saving, Revert and change tracking are unchanged. `SettingsPage` shows only essential categories (Essentials, Limits) until **Show all settings** (`general.show_all_settings`); search covers everything. The governor mode row is in place for phases 5-6.
- The performance profiles are removed (§10). `min_zoom` is no longer on the Imagery page (§6.3, §9); its hidden slider still bounds the maximum zoom.
- Migration notice (§10): converting a pre-sparse file records hand-set engine values in `general.kept_values`; Home shows them (`ui/widgets/notice_bar.py`, the notice area phase 9 builds on) with **Review** and **Use automatic values** (controls found by object name, scaled sliders handled; settings without a control are reset in the Expert table).
- Registry (§6.1): `SettingSpec.owner` (target / limit / user / governed / measured / internal / fixed, as §9) and `live` (true for the call-time streaming options). Per-lever step and cooldown live with the governor (phase 5). `prefetch_pipeline_v2` is marked `retire_after`; `fetch_threads` is already labelled as the HTTP/1.1 fallback.
- Wizard (§7): six steps (checks on Welcome, X-Plane, Scenery with its folder, Image cache with a suggested limit and More folders, Quality, Limits and ready with "Start AutoOrtho when setup finishes").
- Guards: the disk check counts what the cache may still take; fixes can return a custom graphics memory limit to Auto, raise a custom download limit, or lower the disk cache. Low-disk brake (`ram_brake.DiskBrake`) on JPEG cache writes and DDS cache stores.
- Limits: the connection warm start applies only to the provider tested; a bandwidth limit sets `provider_origin_max_concurrency` from the measured per-request rate.
- Not done here: `protect_sim_fps` and measured graphics memory belong to the governor sampler (phases 5 and 9); `disk_cache_gb` remains `cache.file_cache_size` (owner "limit"); `prefetch_pipeline_v2` retires one release later.

### Phase 4 (live levers)

- `autoortho/live_settings.py`: `LiveSettings` publishes lever values from the parent into the stats store (`governor/levers` plus a generation key); each worker polls every 1.5 s (a daemon thread started with the prefetcher), writes changed values into its `CFG` and runs registered hooks. `ResizableGate` is a resizable bounded semaphore.
- getortho hooks: `live_tile_admission` (now a `ResizableGate`), `background_builder_workers` (the pool is created at its ceiling of 16; the coordinator dispatches up to the live value), `predictive_dds_build_interval_ms`, `predictive_dds_memory_mb`, `cache_mem_limit` (the clean loop evicts down to a lowered value), prefetch lookahead / radius / chunks / interval (the prefetcher reloads), `limits.cpu_threads` (native thread budget) and `limits.ram_gb` (the brake's limit). `tile_image_cache_mb` and `cache_write_buffer_mb` are read at use, so a published value applies to new tiles and writes.
- `live_builder_concurrency`, `buffer_pool_size`, `streaming_builder_pool_size`, `provider_max_in_flight` and `provider_origin_initial_concurrency` size pools at start and stay start-only; live builds are bounded live through `live_tile_admission` and the thread budget. OpenMP's per-worker `OMP_THREAD_LIMIT` is fixed at spawn, so a raised CPU limit takes full effect at the next start.
- Broker: a `CONFIG` control message (`HTTP2Broker.configure()`) moves the adaptive controller's ceiling (`set_maximum`, never under its floor, at most the worker count) and the token bucket's rate (0 removes it).
- Parent: `AOMount.publish_levers()` and `apply_live_levers()` (recomputes automatic values with `AOConfig.recompute_automatic()` and publishes what changed); levers are cleared at unmount. The UI applies limits and live Expert edits at once while streaming; `SettingSpec.live` marks what is live, and only non-live Expert edits raise the restart notice.

### Phases 5-6 (governor: Advise and Auto)

- `autoortho/governor.py`. `GovernorService` runs in the parent while streaming (started after the mounts, stopped first at unmount) and samples every 2 s:
  - worker counters from the stats store: `near_reads_real` / `near_reads_standin` (the fidelity metric, counted in `getortho._read_dds_bytes_budgeted`), `req_ok` / `req_err`, `bytes_dl`, `live_admission_waits` / `live_admissions`, `time_to_sharp_ms_*` and the brake counts;
  - the broker's `server_stats()` (origin limit and active count, backlog);
  - psutil CPU and RAM;
  - `datareftrack` (AGL, ground speed, and now `sim/operation/misc/frame_rate_period` and `sim/time/paused`).
- `Governor` is pure (no I/O or clocks of its own):
  - a 30 s `Window`, a classifier with the states of §4.4 plus `sim_fps`, and a move plan per state;
  - one decision at a time, at most every 6 s; per-lever cooldown 30 s (half while starting, four times after a revert); double steps while starting;
  - keep/revert judged 20 s after a change on the samples since the change, against fidelity (−2 points) and the state's own metric (−5 %);
  - degradation shortens prefetch and sets a notice after 5 minutes with every lever for the bottleneck at its limit;
  - eleven levers (`levers_for()`), bounded by the limits: the provider ceiling, live tile admission, background builders and their pause, three memory budgets, two waits, and prefetch lookahead and radius.
- **Advise** was the default until the tuner completion below; **Auto** (the proposal's default) is the default now. Going back is a one-line change to `[governor] mode` in the template.
- Learned profiles: `LearnedProfiles` in `~/.autoortho-data/governor/learned.json`, keyed by `profile_key()` (target hash, provider, `network_fingerprint()`, a salted hash of the local /24 networks). Applied as a warm start in Auto only; saved at unmount in Auto.
- `protect_sim_fps` (`[limits]`, default on). The best 30 s frame-time average after starting is the reference. Frames more than 20 % slower than it, and under 40 fps, classify as `sim_fps`, which lowers background builders, lengthens the build pause and shortens prefetch. The change is kept while fidelity stays ≥ 95 %.
- Transparency:
  - Settings > Auto-Tuning (`ui/pages/tuning_page.py`): the table, timeline and buttons of §5.2, refreshed each second;
  - Home notices for Advise suggestions (**Apply** pins the value) and for an unreachable target;
  - a log line per decision;
  - `governor/summary` in the stats store becomes the report's *Flight verdict* section (`diagnostics._render_flight`).
- Replay: `[governor] record_samples` writes `samples-*.jsonl`. `load_samples()` + `replay()` run the same rules offline (`tests/test_governor.py`).
- Not done here:
  - the fitted paging `k` is not stored in the learned profile (it is fitted in each worker); the measured graphics memory peak is (since phase 8);
  - the far/middle "one level below target" step of §4.4 degradation is left to the existing low-zoom base and wait bands;
  - texture capping is phase 8.

### Phase 7 (wait consolidation)

- The wait bands are the only wait policy. `wait_bands.py` derives what the retired keys did:
  - `chunk_wait_seconds(starting)` replaces `maxwait`: a near read's band wait, so a row may use what is left of the read's band budget;
  - `tile_budget_seconds(initial_load)` replaces `tile_time_budget`: the longest band wait (60 s by default, as before), ×10 up to 30 minutes before X-Plane first connects;
  - `http_read_timeout_seconds()` replaces `maxwait + 1` for the HTTP/1.1 read timeout.
  - `suspend_maxwait` and `use_time_budget` are always on.
- Callers moved:
  - `Tile.get_maxwait()` (every live row wait goes through it and `QualityDeadlinePolicy`), `_get_or_create_tile_time_budget()`, the `get_img()` fallback budget, the two live-transition budgets, the initial-load priority penalty and the healing timeout (a fixed 10 s, its old default);
  - in `autoortho_fuse.py`, the macFUSE `daemon_timeout` and `_calculate_build_timeout()`.
- `maxwait`, `suspend_maxwait`, `use_time_budget` and `tile_time_budget` are `RETIRED` in the registry: kept in the template for old files, ignored, and never written.
- UI: the four legacy controls on the Streaming page became two sliders bound to `[streaming] band_max_wait_seconds` ("Wait for nearby imagery in flight") and `near_wait_seconds` ("Wait while a flight loads"). They are live: a changed value is published to the workers on Apply; an unchanged one is not republished, so the governor's value stands.
- Behaviour change to watch in flight reports: a near row may now wait up to the band wait (6 s in flight, 60 s while starting) instead of `maxwait` (2 s) plus grace.
- Docs: `performance.md` (wait policy, startup loading, recommended configurations, the stale profile presets section), `faq.md` and `config.md`.

### Phase 8 (texture-size lever)

- `autoortho/texture_plan.py`:
  - texture sizes (4096 px at the file zoom, doubling per level);
  - `capped_zoom()`, which lowers a build zoom until the texture fits the cap;
  - `texture_bytes()` (BC1 half a byte a pixel, BC3 one, a third more for the levels);
  - `HeldTextures`, OSXP's graphics memory meter: each texture at the finest level X-Plane read in the last 20 minutes.
- Engine:
  - the cap is a clamp in `TileCacher._get_target_zoom_level()`, so live tiles and the prefetcher agree. It applies to tiles opened after `[governor] texture_cap_px` changes (a live setting), since X-Plane keeps a texture's size while it holds it;
  - the DDS caches are keyed by the build zoom (`{tile}_z{zoom}`), so a capped texture is plan-keyed without a new key;
  - `texture_capped_tiles` counts capped opens;
  - each worker's `_note_wanted_level()` feeds `held_textures`; the clean loop publishes `graphics_memory_bytes:<pid>`, and the governor sums them.
- Downscale before encode is left out. A capped texture is built at the zoom that fits, not downloaded one level finer and shrunk as OSXP does: that costs four times the downloads and decodes for a small sharpness gain, and the 8K buffer pool stays sized for uncapped textures.
- Governor:
  - `allow_texture_cap` adds the `governor.texture_cap_px` lever (0 or 4096);
  - the `vram` state (the estimate at 95 % of `guards.vram_budget()`) caps textures once. That decision is kept without judging (the estimate falls over 20 minutes) and never eased off in headroom;
  - without the permission, the governor sets a notice instead;
  - the summary records `peak_graphics_memory_gb` and `texture_cap_px`.
- UI: "Allow smaller textures when graphics memory is short" on the Auto-Tuning page. It is live: allowing it adds the lever to a running governor; withdrawing it lifts a cap (publishes 0). The status line shows the estimate against the budget.

### Phase 9 (Home revamp)

- `autoortho/home_snapshot.py`: `build()` turns the governor snapshot and the app's state into a `HomeSnapshot`. It is pure, no Qt:
  - state: stopped / starting / flying / paused / after flight;
  - the status strip, the hero and its 5 s trend buckets;
  - the gauges with a level and a bottleneck chip mapped from the governor state;
  - activity, the auto-tuning lines, the radar and the tray/taskbar summary.
  The main window builds it once a second (with the Auto-Tuning refresh) and sends it through one signal (`home_snapshot_ready`).
- Sampler additions (`GovernorService`):
  - session totals since streaming started: textures served (`tile_mem_*`), cache share (`chunk_hit` / `chunk_miss`), bytes downloaded, distance and duration;
  - climbing/descending from the last minute of altitude;
  - heading;
  - workers' gauges summed (`building_now`), maxed (`prefetch_horizon_m`) or joined (`tile_radar`);
  - `flight_summary()` for the last-flight card, saved to `governor/last_flight.json` at unmount.
- New metrics:
  - prefetch horizon: `SpatialPrefetcher._note_horizon()`, every 10 s, the distance along the predicted path before the first tile that is neither in memory at target nor in the DDS cache; velocity mode only;
  - builds in progress (live tile admission held);
  - graphics memory (phase 8's `HeldTextures`);
  - loading progress (the governor's near-read fidelity while X-Plane is not yet connected);
  - the tile radar (`TileCacher._publish_radar()`: the 300 nearest open tiles within 60 nm and their state, every 10 s).
- `ui/pages/home_page.py`:
  - status strip; notices (`NoticeBar`); stopped blocks (one-line readiness, target with guard lights, storage, scenery); starting block; flying block (clickable hero with `SparkLine`, three `GaugeCard`s, activity with `TileRadar`, auto-tuning); last-flight card; footer;
  - paused dims (opacity effect) and freezes the numbers;
  - the old setters stay as the compatibility API: the task and failure cards became a badge on Scenery library and a `scenery_task_failed` notice.
- Optional extras:
  - tray summary: `general.hide` was a setting without an implementation; it now minimizes to a tray icon while streaming, with the summary as its tooltip ("!" in front while a warning notice waits), and the window title carries the summary too;
  - compact mode, which also hides the navigation rail;
  - the tile radar.
- Accessibility: every gauge, the hero, the radar and the last-flight card have a full sentence as their accessible description; state is shown by symbol and text as well as colour (●, ◐, ❚❚, ■, ◆, ✓, !); blocks are added in reading order.
- Not done:
  - "Open map with blurry spots" (the map has no layer for them yet; the card opens the map);
  - the horizon on SimBrief routes;
  - the tray icon's colour by state (the tooltip carries it).

### Follow-ups from the OrthoStreamXP comparison

A review against OSXP's streaming and tuning code found three gaps, now closed:

- **Live tile admission only for detailed builds.** Every DDS read used to take one of the `live_tile_admission` slots for the whole read, waits included. While a flight loads, near reads may wait 60 s, so 16 of them could hold every slot and leave header chunks and far reads to time out into the fallback texture. `read_pattern.needs_build_slot()` (called through `Tile.needs_live_slot()` in `autoortho_fuse.read`) lets the DDS header, the header chunk and its FUSE pieces, and base-level reads skip the gate; `live_admission_skipped` counts them. With `read_classifier` off every read is admitted, as before. This is AutoOrtho's version of OSXP's weighted build gate: the slots still bound detailed builds, and reads that take no build memory don't queue for them.
- **Waits raised on evidence.** `getortho.LateArrivals` watches the chunks a near or middle read gave up on and counts how long they took from the start of the read, in `wait_bands.LATE_BUCKETS_S` (`late_flight_b*`, `late_start_b*`). `Governor.wait_evidence()` raises `band_max_wait_seconds` (or `near_wait_seconds` while starting) only when at least 20 (5 while starting) such reads were seen and 80 % (90 %) of them arrived within the lever's maximum, and never beyond that time (OSXP's `Tuner` rules). When most took longer, the new state `slow_imagery` lengthens prefetch lookahead instead. Without evidence the state falls through to `behind`.
- **Applied Advise suggestions are learned, not pinned.** **Apply** publishes the value, records it in `LearnedProfiles.put_applied()` (the `applied` part of the profile entry, kept when Auto saves its levers) and leaves the setting unpinned, so the governor may move it again and **Reset learned settings** clears it (OSXP's tuned store). `LearnedProfiles.warm_start()` gives Advise flights the applied values and Auto flights the applied values overlaid with what Auto settled on. The Tuning page marks these values *Learned*.

Still open: persisting the fitted paging `k` (low value: it is refitted after 200 reads).

### Behind the aircraft, by evidence

Implemented from [governor-behind-detection-plan.md](governor-behind-detection-plan.md); the blind `behind` fallback (lookahead and radius raised on low fidelity alone) is gone.

- `autoortho/prefetch_ledger.py`: `PrefetchLedger` per worker (`getortho.tile_ledger`), keyed by `(row, col, file zoom)` without the maptype, bounded (8192 entries, 20 min TTL). `classify_miss()` returns never / evicted / coarse / late / done; `never_kind()` splits a never-queued miss into paused, turn (track changed > 30° in 60 s), wide (> 0.8 × the prefetch radius off the track) or inside, in that order, so each miss counts once.
- Hooks (`getortho.py`, `prefetch.py`):
  - `_prefetch_tile(source=…)` notes the queue (velocity, simbrief, paging, ter, dsf) after it submitted chunks or found nothing left to submit, and notes the level done when every chunk in its range was already in. A `max_chunks` cut no longer returns early, so a truncated submission is noted too;
  - `PrefetchCoordinator(on_publish=…, on_complete=…)` covers every coordinator source (velocity, SimBrief and DSF with the bounded pipeline), not only DSF;
  - `_on_tile_complete_callback` (all mipmap-0 chunks in) marks full detail done; eviction passes the tile's `real_from()` so a tile completed without the tracker still counts as `evicted`;
  - `SpatialPrefetcher._note_pause()` records skipped cycles (RAM brake, live building or live pressure, runtime gate). A pause within 60 s explains a never-queued miss (the plan said one prefetch interval; a near tile would have been queued tens of seconds before its read);
  - `Tile._note_near_miss()` on a blurry near read bumps `near_miss_<kind>` (and `near_miss_paused_<reason>`), skipped while starting, without a position and below the prefetcher's 25 m/s.
- Governor: eight `near_miss_*` sample fields plus the pause reasons; `Window.miss_counts`. Below `FIDELITY_LOW` with no wait verdict, `miss_state()` needs `MIN_MISSES` (10) and one cause behind 50 % of them; states `behind_wide` (radius), `behind_short` (lookahead), `paging_coarse` (`streaming.paging_lead_seconds`, new lever 30-180 s), `prefetch_late` (lead, then `prefetch_max_chunks`, new lever 64-2048; none while the line is full), `memory_evicted` (memory cache, only under 80 % of the RAM limit), `prefetch_paused` (no move) and `unexplained` (no move). The two no-move states set a notice after five minutes of recurring.
- Keep/revert for the miss states uses that cause's share of **near reads** rather than of misses, so a change that cut every cause is not judged worse.
- `Sample.starting` is recorded, so `replay()` judges each sample as the live governor did (old recordings: unchanged).
- Report: *Blurry moments by cause* by phase (`diagnostics._render_miss_causes`); `summary()` has `misses` and `misses_by_phase`, the timeline the window's miss counts.
- The thresholds (`MIN_MISSES`, `MISS_DOMINANT_SHARE`, `WIDE_SHARE`) are the plan's starting values. The governor still ships in Advise, so the reports from the first flights can set them before Auto acts on them.

### Tuner completion

A review of the automatic configuration found it reactive only (no starting
point from the target or the line), two bugs, and loose spots. Changes:

- **Tuning plan** (`tuning_plan.py`, applied in `AOConfig._apply_hardware_baseline`
  after the hardware baseline and before the limits, source `planned`):
  `target_cost`'s busiest-phase need against the last connection test of the
  provider (capped by the bandwidth limit) sets prefetch chunks per cycle,
  prefetch radius, lookahead and paging lead, both waits (only raised) and
  predictive build memory (only raised). The download ceiling
  (`provider_origin_max_concurrency`) is sized from min(line, bandwidth
  limit) with the test's per-request speed. Every planned value lies inside
  the governor's lever range. Same switch as the baseline
  (`[tuning] hardware_baseline`). Workers load the config themselves and
  compute the same plan; `start_governor` calls `recompute_automatic` so the
  parent matches them.
- **Bug: concurrency 0.** `provider_origin_max_concurrency = 0` means "the
  provider's ceiling", but the governor stepped from the number 0, so a
  "raise" clamped to the 64 floor and cut the ceiling from 320 to 64.
  `governor.lever_state()` now turns 0 into the lever's maximum and keeps
  automatic values inside the lever ranges (corrections are published at
  start).
- **Bug: degradation was never published.** `_note_limited` lowered
  prefetch levers in `values` only, so workers never saw it while the
  learned profile stored it. Each step is now a `Decision` (state
  `unreachable`, outcome `kept`, not judged), published in Auto and listed
  in Advise; headroom eases it back once the scenery is sharp.
- **CPU.** The service samples AutoOrtho's process tree
  (`ProcessTreeCpu`, share of the PC) and the governor compares it with the
  share the CPU-core limit allows (`ao_cpu_budget_percent`); the whole-PC
  90 % still counts as "no CPU to give". Headroom uses AutoOrtho's share.
- **Network.** Samples carry the broker's summed ceilings. A saturated queue
  raises the ceiling only when the adaptive limit sits at it
  (`network_room`); below it the server sets the pace (`network_paced`,
  narrows prefetch). `network_full` and `network_paced` act only below 98 %
  sharp.
- **Keep or revert.** Judged from 10 s with 60+ near reads, else at 20 s;
  waits up to 60 s for near reads in the same flight phase, then keeps.
  Fidelity counts as worse only when it fell by more than 2 points and two
  standard errors of the difference. A kept change is not eased back by
  headroom for 120 s.
- **Convergence.** One extra step's worth while under 85 % sharp (on top of
  the doubled steps while starting); the plan supplies the starting point.
- **Mid-flight changes.** `AOMount.sync_governor()` (after Apply of limits
  and after live Expert edits) rebuilds levers, ranges, pins and bounds via
  `Governor.reconfigure()`; values whose automatic value the new limits
  changed are adopted, tuned values are clamped into the new ranges, a
  pending verdict is dropped, and in Auto the governor's values are
  published again.
- **Learning** stores only levers that settled away from their baseline, so
  later plan improvements are not masked by stored copies of old defaults.
- **Build slots.** `live_builder_concurrency` was start-only and only sized
  the DDS buffer pool (capped by `buffer_pool_size`, default 10) and the
  decoder pool; the streaming builder pool ignored it (fixed 4). Raising
  `live_tile_admission` past those pools only queued tiles at them, which
  the governor could not see. Now both pools are resizable
  (`DDSBufferPool.resize`, `StreamingBuilderPool.resize`) and follow
  background + live builders (`AoDDS.build_slot_count`) unless the user set
  their size; DDS buffers stay within a quarter of the RAM limit. Live
  builds count build-slot acquisitions, waits (> 10 ms) and timeouts
  (`getortho.note_build_slot`); a build-wait share over 20 % is the
  `build_slots` state (checked before the admission rule), which raises the
  new `Live builders` lever (ceiling: cores + 2, and the 8K buffers a
  quarter of the RAM limit holds). The decoder pool is sized for 48
  builders (decoders are created on demand). Also fixed: a buffer handed to
  a waiter just as its wait timed out was leaked.
- **Plan in flight reports.** The flight summary's `plan` holds the target,
  need, line, ratio and, per planned setting, the planned and settled values
  (`AOMount._settled_plan`); the report renders it as a *Tuning plan* table.
  Many reports side by side show which plan rule is off and by how much.
