# WinNative Turnip Drivers

A unified [Turnip](https://docs.mesa3d.org/drivers/freedreno.html) Vulkan driver build for **all Adreno GPUs on Android**, packaged as Adrenotools-compatible `.zip` archives. The build script always pulls upstream Mesa main, applies the patches in `patches/`, and produces three flavours of the same `libvulkan_freedreno.so`.

## Variants

| Variant | File | Tuning |
|---------|------|--------|
| Balanced | `WN-Turnip-<version>-b_Axxx.zip` | Slightly relaxed GMEM autotuner. Standard KGSL power management. Best for most games and battery life. |
| Eco | `WN-Turnip-<version>-e_Axxx.zip` | **A8xx/A840-tuned.** Defaults a8xx to the adaptive `bandwidth` autotuner instead of upstream's hardcoded `prefer_sysmem` for DX titles, which disables tiled rendering outright. Exposes two on-device tunables so tuning does not cost a build per value: `TU_ECO_DEPTH_CACHE_KB` (64/128/192/256 - trade GMEM depth-cache capacity for tile pixels; 192 makes a 1080p colour+depth pass fit in a single tile on A840) and `TU_ECO_GMEM_BW_NUM` (the GMEM penalty in the bandwidth estimate, default 11/10). Logs the GMEM budget under `TU_DEBUG=startup`. Aimed at joules-per-frame, not peak fps. |
| Performance | `WN-Turnip-<version>-p_Axxx.zip` | Aggressive GMEM autotuner. Forces `KGSL_PROP_PWR_CONSTRAINT = PWR_MAX` at queue creation and re-asserts it every 1000 submissions to keep the GPU at top clocks. Higher framerate, higher power draw. |

Each archive ships:

```
libvulkan_freedreno.so
meta.json
```

## Supported GPUs

A single driver covers the full Adreno line:

- **A6xx** — A6xx series (e.g. A640 / A650 / A660)
- **A7xx**
  - `gen1` — A720 / A725 / A730 (preamble + scalar-predicate quirks applied)
  - `gen2` — FD740 / Adreno X1-85 (UBWC hint applied to fix UI corruption)
  - `gen3` — A750 and later
- **A8xx** — A810 / A825 / A829 / A830 / A840 (and X2-85)
  - A810 / A829 boot with `disable_gmem` so they fall back to sysmem rendering (broken GMEM on those parts).
  - A825 (not yet upstream) is added with corrected tile geometry and depth-cache layout.
  - A830 / A840 / X2-85 use the upstream profiles unchanged.

## Features

- Always built from **upstream Mesa main** — every run re-clones, re-patches, re-builds. No pinned forks.
- Per-chip `disable_gmem` GPU property plumbed through `freedreno_dev_info.h` and `tu_cmd_buffer.cc` for parts with broken GMEM.
- KGSL UBWC gralloc detection bypass (newer Qualcomm gralloc no longer writes the legacy `gmsm` magic header).
- A8xx UE5 / VKD3D-Proton SM6.6 freeze workaround — `EXT_shader_image_atomic_int64` advertisement disabled while upstream resolves the underlying 64-bit image atomic implementation.
- Adrenotools-compatible `meta.json` with simple `WN-<version>-<variant>` driver versioning.

## Build

Requirements:

- Linux host (Ubuntu / Debian recommended)
- `git`, `meson`, `ninja`, `patchelf`, `unzip`, `curl`, `pip`, `flex`, `bison`, `zip`, `glslang` / `glslangValidator`
- Python with `mako`
- Android NDK r26d (the script expects it at `/home/max/Build/Turnip/android-ndk-r26d` — edit `build_turnip.sh` to point at your NDK)

```bash
./build_wn_turnip.sh
```

Outputs both variants as `WN-Turnip-<version>-{b,p}_Axxx.zip` in the project root.

## Install on device

Adrenotools-aware launchers (e.g. WinNative, Winlator) can import each `.zip` directly:

> Container → Edit → Graphics Driver → Import driver → pick the `.zip` → save → relaunch.

## Layout

```
.
├── build_wn_turnip.sh        # entrypoint: builds both -b and -p
├── build_turnip.sh           # core cross-compile engine (do not call directly)
├── patches/
│   ├── fix_gralloc_flushall.py
│   ├── fix_a8xx_dev_info.py
│   ├── apply_a8xx_gpus.py
│   ├── apply_a7xx_gen1_quirks.py
│   ├── apply_a7xx_gen2_ubwc_hint.py
│   ├── apply_balance_variant.py
│   ├── apply_perf_variant.py
│   └── disable_64b_image_atomics.py
├── MAINTENANCE.md            # per-patch upstream status + re-verify checklist
└── LICENSE
```

## Upstream & contributing

Development happens on the fork [`maxjivi05/Drivers`](https://github.com/maxjivi05/Drivers)
and is contributed to the main repo
[`WinNative-Emu/Drivers`](https://github.com/WinNative-Emu/Drivers) via pull request.
Because the build always tracks upstream Mesa main, the `patches/` scripts are written
to adapt as upstream changes — see [`MAINTENANCE.md`](MAINTENANCE.md) for each patch's
current status, what upstream has absorbed, and how to re-verify on a Mesa bump.

## License

MIT — see `LICENSE`.
