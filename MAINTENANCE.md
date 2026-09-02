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

## Patch status — verified against mesa `d870cef8b7c` (Mesa 26.3.0-devel, 2026-09-02)

| Script | Target / anchor | Status | Notes |
|--------|-----------------|--------|-------|
| `fix_a8xx_dev_info.py` | `freedreno_dev_info.h` `disable_gmem` prop + `tu_cmd_buffer.cc` no_gmem check | **needed** | Upstream has render-pass-scoped `disable_gmem`, but **no per-GPU** flag. Anchor `bool has_image_processing;` present. The injected block picks its reason field from `REASON_FIELDS` — upstream renamed `tu_render_pass_state::gmem_disable_reason` to `force_render_mode_reason` after 2026-08-26 and the old hardcoded name broke the build. |
| `apply_a8xx_gpus.py` | `freedreno_devices.py` A810 / A829 / A825 | **needed** | A810+A829 get `disable_gmem=True` + KGSL chip_ids; **A825 not upstream** (fully injected). |
| `apply_a7xx_gen1_quirks.py` | `a7xx_gen1` GPUProps | **needed** | Forces `has_early_preamble/has_scalar_predicates=False` for A720/725/730. |
| `apply_a7xx_gen2_ubwc_hint.py` | X1-85 / FD740 add_gpus block | **needed** | Adds `enable_tp_ubwc_flag_hint`. That block still lacks it upstream. |
| `disable_64b_image_atomics.py` | `has_64b_image_atomics = True` (×2, gen2+gen3) | **retired — not in `EXTRA_SCRIPT`** | Kept on disk as a one-line revert. See below. |
| `add_aimapper_gralloc.py` | new file + `u_gralloc.c` selection table, `u_gralloc.h` enum, `u_gralloc_internal.h`, `meson.build` | **needed** | IMapper5 backend via SP-HAL; still nothing like it upstream (mesa has zero hits for `android_load_sphal_library`). Anchors are small additive edits; if `u_gralloc` is restructured upstream, re-diff the selection table. The backend must keep filling every field of `u_gralloc_buffer_basic_info` — mesa `b3bb742f` added `alloc_size` and `layer_count` and `d850d2ef` consumes them. |
| `add_ubwc_swapchain_usage.py` | `vk_android.c` `vk_android_get_ahb_image_properties()` `ahb_usage_props` assignment; `vk_physical_device.h` struct tail; `tu_device.cc` after `supported_sync_types` | **needed** | The vendor UBWC usage bit, and nothing else any more. Value is **driver-set** (`ahb_vendor_usage_compressed`) so no Qualcomm constant lands in shared code. **Must stay at that assignment** - the output-chained `VkAndroidHardwareBufferUsageANDROID` is what distinguishes an allocation query from import validation; putting it in `vk_image_info_to_ahb_usage()` breaks AHB *import* for every linear buffer. Re-anchored 2026-09-02 for mesa `b1bf53eb`, which renamed and restructured the old `if (ahb_usage)` block. |
| `apply_balance_variant.py` (-b) | `tu_autotune.cc` drawcall + bandwidth | **partial** | Only the `*11→*10` bandwidth tweak lands; the `> 5` drawcall anchor was **removed upstream** (now `>= 10`) and is skipped with an explicit line. |
| `apply_perf_variant.py` (-p) | `tu_autotune.cc` + `tu_knl_kgsl.cc` PWR_MAX | **needed** | All five KGSL `PWR_MAX` anchors present. Same autotune drawcall skip as -b. Every edit reports applied / already / absent — it used to print only on success, so a drifted KGSL anchor would have shipped a -p with no clock forcing and no warning. |

### Absorbed / removed by upstream (do NOT re-add)
- **`fix_gralloc_flushall.py`** — removed 2026-08-07. It patched `u_gralloc_fallback.c`,
  which `add_aimapper_gralloc.py` makes unreachable: the AIMapper backend is selected first
  (device log `Using IMapper v5 stable-C API via SP-HAL`), so the fallback never runs. Kept
  only as build fragility — a missing anchor in an unused file would have failed the build.
- **The mutable-format UBWC gate and the DISABLED check inside
  `add_ubwc_swapchain_usage.py`** — removed 2026-09-02. Mesa `491bb61a` / `b1bf53eb` /
  `24c8a889` rewrote the Android AHB usage path and now carry the same "Android 16/17 never
  forwards `VkImageFormatListCreateInfo` to this query, so assume optimal tiling when it is
  absent" workaround this patch used to implement with a `vk_physical_device` property, so
  `mutable_format_compression_compatible` is gone. `e39a340f` makes a
  `VK_IMAGE_COMPRESSION_DISABLED_EXT` request come out as `CPU_WRITE_RARELY`, which the
  patch's existing CPU-usage guard already rejects — so compression control is honoured
  without the patch looking for the struct. Upstream's mutable rule is slightly *bolder* than
  ours was (it does not additionally require `ubwc_all_formats_compatible`), but on a840 that
  property is true via `a7xx_gen3`, so the two agree on this hardware.
- **`TU_DEBUG_FLUSHALL` forced for gen8** — upstream removed the forced flush from
  `tu_device.cc`. The old `fix_gralloc_flushall.py` half that stripped it is gone.
- **Autotune `drawcall_count > 5` gate** — restructured upstream to `>= 10`. The -b/-p
  scripts skip this tweak cleanly; the two variants now differ by **bandwidth + PWR_MAX**,
  not the drawcall threshold. (Re-target to the new gate only if a split is desired.)

### Retired patches
- **`disable_64b_image_atomics.py`** — dropped from `EXTRA_SCRIPT` in 1.12. It cleared
  `has_64b_image_atomics` on `a7xx_gen2` **and** `a7xx_gen3` (which every A8xx inherits),
  which removes `VK_EXT_shader_image_atomic_int64` / `shaderImageInt64Atomics`. That is the
  feature upstream added in `5b87bbfad3b` specifically "for SM6.6 in vkd3d-proton", so with
  it off, VKD3D-Proton reports `Options9.AtomicInt64OnTypedResourceSupported = FALSE` and
  rejects any pipeline whose DXIL uses typed 64-bit image atomics — Hogwarts Legacy and
  FF VII Rebirth among them. 1.12-test builds with it removed were confirmed working on
  device. The script stays on disk: if the A8xx post-submit GPU hang it was written for
  returns, re-append `:patches/disable_64b_image_atomics.py` to `EXTRA_SCRIPT` in
  `build_wn_turnip.sh`.

### Removal criteria to watch on future bumps
- **`apply_a8xx_gpus.py` A825 block**: drop the A825 insertion if upstream adds A825
  natively (the script already detects `name="Adreno (TM) 825"` / `FD825` and skips).
  As of mesa `d870cef8b7c` upstream carries A810, A829 (KGSL `0x44030a20`), A830 + A830v1
  (`2061a5ee`), A840, X2-85 and X2-90 natively — **A825 is the only one still fully injected**,
  and the A810/A829 work is now down to `disable_gmem` plus extra KGSL chip_ids.
- **`add_ubwc_swapchain_usage.py`**: drop it if mesa grows its own vendor AHB usage concept.
  The script already detects an upstream `ahb_vendor_usage_compressed` and steps aside.
- **`fix_a8xx_dev_info.py`**: if upstream adds a per-device GMEM-disable mechanism,
  migrate A810/A829 to it and retire the custom `disable_gmem` prop.

## Re-verifying on a mesa bump

**Do this locally first — it costs seconds, a CI build costs five minutes.**

```sh
# a sparse mesa checkout is enough; src/util is REQUIRED or add_aimapper_gralloc.py
# silently reports "nothing to do"
cd <mesa-ref> && git fetch --depth=1 origin main && git checkout --detach FETCH_HEAD
cp -a <mesa-ref>/src /tmp/dryrun/ && cd /tmp/dryrun
git init . && git add -A -f && git commit -m snap   # fix_a8xx_dev_info.py runs
                                                    # `git checkout --` and exits 1 outside a repo
for s in <patches>/*.py; do python3 "$s"; done      # then run the whole set a SECOND time
```

Every script must print an explicit **applied** / **already applied** / **anchor absent**
line on both passes. A bare or missing line is the bug — it means an edit silently did
nothing and the zip will not be what its name says.

Then, on CI:
1. `BUILD_VERSION=<ver> ./build_wn_turnip.sh` (clones latest main, applies patches).
2. Read `build_log_{b,p}.txt`: every script should print an "applied" or an explicit
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
  2026-07-08) builds `-b`/`-p` from latest mesa main and **tags + releases** the
  bumped version. Runs every week regardless of whether this repo changed, since
  mesa main advances on its own.
- **`workflow_dispatch`** takes two extra inputs:
  - `variants` (default `b p`) — build a subset for faster driver-tuning iteration.
  - `publish` (default **false**) — when false, produces artifacts labelled
    `<ver>-test` and **never tags or releases**. Set true to cut an actual release.
    Screening builds should always leave this false.
- **PR / push** build a preview label only — never tag, never release.

**Trap — `workflow_dispatch` builds `main` unless you type the ref.** The `ref` input
defaults to `main`, and the checkout honours it regardless of which branch page the run was
started from. A dispatch that leaves it blank builds `main` while labelling the artifact
exactly as a branch build would, and the only way to tell afterwards is that the log shows
`main`'s `EXTRA_SCRIPT` list. This cost a debugging round on 2026-08-28 (run `33203456475`).
The default is left alone because `main` is the right default for a release run — instead,
**screen through a pull request**: the `pull_request` trigger checks out
`github.event.pull_request.head.sha`, so it always builds the branch, and the `release` job
cannot fire on that event.

Local builds set the label directly, e.g. `BUILD_VERSION=1.03 ./build_wn_turnip.sh`.

## Repository / contribution flow
This is developed on the fork **`maxjivi05/Drivers`** and contributed upstream to the
main repo **`WinNative-Emu/Drivers`** via pull request. Build/patch changes land on a
branch in the fork, then a PR is opened against `WinNative-Emu/Drivers:main`.
