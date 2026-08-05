# Maintenance — patch review & upstream tracking

This repo always builds from **upstream mesa main**, then applies the idempotent
Python scripts in `patches/`. Because upstream moves, every patch must be able to
tell three states apart and act accordingly:

1. **Already applied** (our change is present) → log "already …", do nothing.
2. **Anchor present** (upstream unchanged) → apply the change.
3. **Anchor absent** (upstream refactored/absorbed it) → log a clear "anchor absent /
   upstream absorbed — skipping" line and exit 0. **Never** silently no-op, and
   **never** `sys.exit(1)` for an absorbed change (that would break the build).

All `patches/*.py` are idempotent and safe to re-run.

## Patch status — verified against mesa `43094891c9b` (Mesa 26.2.0-devel, 2026-07-01)

| Script | Target / anchor | Status | Notes |
|--------|-----------------|--------|-------|
| `fix_gralloc_flushall.py` | `u_gralloc_fallback.c` gmsm block | **needed** | UBWC detection for newer Qualcomm gralloc. Anchor present. |
| `fix_a8xx_dev_info.py` | `freedreno_dev_info.h` `disable_gmem` prop + `tu_cmd_buffer.cc` no_gmem check | **needed** | Upstream has render-pass-scoped `disable_gmem`, but **no per-GPU** flag. Anchor `bool has_image_processing;` present. |
| `apply_a8xx_gpus.py` | `freedreno_devices.py` A810 / A829 / A825 | **needed** | A810+A829 get `disable_gmem=True` + KGSL chip_ids; **A825 not upstream** (fully injected). |
| `apply_a7xx_gen1_quirks.py` | `a7xx_gen1` GPUProps | **needed** | Forces `has_early_preamble/has_scalar_predicates=False` for A720/725/730. |
| `apply_a7xx_gen2_ubwc_hint.py` | X1-85 / FD740 add_gpus block | **needed** | Adds `enable_tp_ubwc_flag_hint`. That block still lacks it upstream. |
| `disable_64b_image_atomics.py` | `has_64b_image_atomics = True` (×2, gen2+gen3) | **needed (workaround)** | UE5/VKD3D-Proton SM6.6 A8xx GPU-hang workaround. See removal criteria below. |
| `apply_balance_variant.py` (-b) | `tu_autotune.cc` drawcall + bandwidth | **partial** | Only the `*11→*10` bandwidth tweak lands; the `> 5` drawcall anchor was **removed upstream** (now `>= 10`) and is skipped. |
| `apply_perf_variant.py` (-p) | `tu_autotune.cc` + `tu_knl_kgsl.cc` PWR_MAX | **needed** | KGSL PWR_MAX clock-forcing anchors all present. Same autotune drawcall skip as -b. |
| `apply_eco_variant.py` (-e) | `tu_device.cc` gmem setup + `tu_autotune.cc` bandwidth estimate | **needed** | A8xx-gated. Adds GMEM budget logging plus the `TU_ECO_DEPTH_CACHE_KB` and `TU_ECO_GMEM_BW_NUM` tunables. Autotune default matches upstream (`prefer_sysmem`) — see notes below. |

### Absorbed / removed by upstream (do NOT re-add)
- **`TU_DEBUG_FLUSHALL` forced for gen8** — upstream removed the forced flush from
  `tu_device.cc`. The old `fix_gralloc_flushall.py` half that stripped it is gone.
- **Autotune `drawcall_count > 5` gate** — restructured upstream to `>= 10`. The -b/-p
  scripts skip this tweak cleanly; the two variants now differ by **bandwidth + PWR_MAX**,
  not the drawcall threshold. (Re-target to the new gate only if a split is desired.)

### `apply_eco_variant.py` (-e) — notes
Two anchors, both fragile in different ways:

- **`tu_device.cc` gmem setup.** The anchor is the `device->dev_info = info;` …
  `fd6_calc_gmem_cache_offsets(&info, …)` block. The patch **rewrites the call to pass
  `&device->dev_info` instead of `&info`** — the local `info` is `const`, so the depth-cache
  override has to be applied to the mutable copy and the offsets computed from that same copy.
  If upstream changes either the assignment order or the call argument, re-diff carefully:
  a version that still compiles but reads the *unmodified* struct would silently make
  `TU_ECO_DEPTH_CACHE_KB` a no-op.
- **`tu_autotune.cc` bandwidth estimate.** Anchored on the literal
  `gmem_bandwidth = (gmem_bandwidth * 11 + total_draw_call_bandwidth) / 10;`. If upstream retunes
  that constant the anchor vanishes and `TU_ECO_GMEM_BW_NUM` is skipped — which is correct
  behaviour, but re-diff before assuming the old default still applies.

The `tu_device.cc` change is A8xx-gated (`fd_dev_gen(...) == 8`), so a drift that mis-fires
cannot affect A6xx/A7xx users.

**`TU_ECO_DEPTH_CACHE_KB` accepts only 64/128/192/256.** The CCU depth cache is described twice
and the two must agree: `gmem_ccu_depth_cache_fraction` is what programs `RB_CCU_CACHE_CNTL`
(`tu_cmd_buffer.cc`), while `gmem_per_ccu_depth_cache_size` only feeds the GMEM offset maths in
`fd6_gmem_cache.h`. Setting the size alone leaves the hardware using a larger cache than the
region reserved for it, and it writes over tile memory. Full depth CCU capacity on a8xx_gen2 is
256 KB/CCU, so the legal points are QUARTER(2)=64, HALF(1)=128, THREE_QUARTER(3)=192, FULL(0)=256.
Confirmed on an A840: gmem_size=18874368, usable=16564224, 674 blocks. 192 KB gives 690 blocks,
enough for a tile-aligned 1080p colour+depth pass in one tile instead of two.

**Why this variant exists:** to expose A840 GMEM behaviour for measurement. The depth CCU cache
(256 KB × 6 CCU = 1.5 MB) is the largest single carve-out from GMEM, and on A840 a 1080p
colour+depth pass lands ~0.9% short of fitting in one bin — so the trade is worth being able to
sweep. The startup logging is what proved the arithmetic on real hardware.

**The a8xx `bandwidth` autotune default was removed on 2026-08-04. Do not reinstate it without
new evidence.** It was measured on an A840 and lost: Cyberpunk 2077 +1.05% fps (inside a 37%
run-to-run spread, i.e. noise), and Subnautica static — the deliberate best case for tiling —
−3.64% fps, +3.88% J/frame, +5.07% frametime. The logs show why: `prefer_sysmem` reports
`Metric Flags: 0x0`, an unconditional early return collecting nothing, while `bandwidth` reports
`0x2 (SAMPLES)` and does real per-renderpass work. When the heuristic lands on sysmem anyway you
pay to decide and then render identically. Root cause: GMEM saves DRAM round-trips, but these
workloads are shader-bound at ~90% GPU busy, so bandwidth is not the constraint. Upstream's
default is not just conservative here — it is cheaper. `TU_AUTOTUNE_ALGO=bandwidth` still selects
it by hand.

**Note for anyone comparing on-device:** WinNative's stock container env includes
`TU_DEBUG=noconform,sysmem`, and `TU_DEBUG_SYSMEM` hard-forces sysmem in `tu_cmd_buffer.cc`,
bypassing driconf *and* the autotuner. Any GMEM-related A/B must drop `sysmem` from that string
or it is measuring nothing.

### Removal criteria to watch on future bumps
- **`disable_64b_image_atomics.py`**: drop once upstream fixes the A8xx 64-bit image
  atomic implementation (track follow-ups to `5b87bbfad3b`). Until then, keep — the
  feature is still advertised `True` on gen2/gen3.
- **`apply_a8xx_gpus.py` A825 block**: drop the A825 insertion if upstream adds A825
  natively (the script already detects `name="Adreno (TM) 825"` / `FD825` and skips).
- **`fix_a8xx_dev_info.py`**: if upstream adds a per-device GMEM-disable mechanism,
  migrate A810/A829 to it and retire the custom `disable_gmem` prop.

## Re-verifying on a mesa bump
1. `BUILD_VERSION=<ver> ./build_wn_turnip.sh` (clones latest main, applies patches).
2. Read `build_log_{b,e,p}.txt`: every script should print an "applied" or an explicit
   "already/absent/skipping" line. A bare/missing line or a `WARNING:` means an anchor
   drifted — re-diff that script against current upstream before shipping.
3. Update the table above with the new mesa hash.

## Versioning

Releases use the scheme **`v1.NN`** — a two-digit, zero-padded counter after
`1.` (`v1.03`, `v1.04` … `v1.99`). The next version is **the latest published
`v1.NN` release + 1** (draft/prerelease releases are ignored); with no release
yet it floors at `1.03`. So `1.02` released → next `1.03`, `1.09` → `1.10`, etc.

The CI (`.github/workflows/build.yml`):
- **Weekly schedule** (`cron: '0 12 * * 3'`, Wednesdays 12:00 UTC, first run
  2026-07-08) builds `-b`/`-e`/`-p` from latest mesa main and **tags + releases** the
  bumped version. Runs every week regardless of whether this repo changed, since
  mesa main advances on its own.
- **`workflow_dispatch`** takes two extra inputs:
  - `variants` (default `b e p`) — build a subset for faster driver-tuning iteration.
  - `publish` (default **false**) — when false, produces artifacts labelled
    `<ver>-test` and **never tags or releases**. Set true to cut an actual release.
    Screening builds should always leave this false.
- **PR / push** build a preview label only — never tag, never release.

Local builds set the label directly, e.g. `BUILD_VERSION=1.03 ./build_wn_turnip.sh`.

## Repository / contribution flow
This is developed on the fork **`maxjivi05/Drivers`** and contributed upstream to the
main repo **`WinNative-Emu/Drivers`** via pull request. Build/patch changes land on a
branch in the fork, then a PR is opened against `WinNative-Emu/Drivers:main`.
