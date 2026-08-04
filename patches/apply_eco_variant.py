#!/usr/bin/env python3
"""
Apply efficiency-oriented changes for the -e (ECO) variant.

Target hardware is A8xx, specifically the Adreno 840 (Snapdragon 8 Elite Gen 5),
which has 18 MB of GMEM -- an order of magnitude more than the A6xx parts most of
upstream's tiling heuristics were tuned against.

Three changes, all A8xx-gated so no other part is affected:

1. tu_device.cc  -- log the GMEM budget under TU_DEBUG=startup. Every tuning
   decision downstream depends on the real gmem_size, which is read from KGSL at
   runtime and is not knowable from source.

2. tu_device.cc  -- TU_ECO_DEPTH_CACHE_KB override for the per-CCU GMEM depth
   cache. Upstream reserves 256 KB x 6 CCU = 1.5 MB of GMEM for depth CCU cache,
   which is the single largest slice of the 2.31 MB carved out before tiling gets
   any. Confirmed on an A840: gmem_size=18874368, usable=16564224, 674 blocks ->
   2,070,528 pixels for a colour+depth pass, against 2,088,960 needed for a
   tile-aligned 1080p -- it misses single-tile rendering by 0.88%. Dropping to
   192 KB (THREE_QUARTER) yields 690 blocks / 2,119,680 pixels and clears it.
   Accepts only 64/128/192/256 because the fraction enum and the byte size
   describe the same cache and must agree -- see the comment at the call site.

3. tu_autotune.cc -- default A8xx to the adaptive `bandwidth` algorithm.
   Upstream's 00-turnip-defaults.conf hardcodes `prefer_sysmem` for DXVK/vkd3d,
   which is an unconditional early return with no metrics collected at all -- on
   this hardware it disables tiled rendering outright. That was a call made for
   parts with 1-3 MB of GMEM. TU_AUTOTUNE_ALGO still takes priority, so A/B
   testing remains possible.

Idempotent and drift-tolerant per MAINTENANCE.md: each change reports
applied / already-applied / anchor-absent, and an absent anchor is logged and
skipped rather than failing the build.
"""

import sys

DEVICE_CC = "src/freedreno/vulkan/tu_device.cc"
AUTOTUNE_CC = "src/freedreno/vulkan/tu_autotune.cc"

changed_any = False


def read(path):
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        print(f"  {path}: file absent (upstream moved it?) — skipping")
        return None


def write(path, content):
    with open(path, "w") as f:
        f.write(content)


# ── 1 + 2: GMEM budget logging and tunable depth cache ───────────────────────

GMEM_ANCHOR = """      device->dev_info = info;
      device->info = &device->dev_info;

      device->usable_gmem_size_gmem =
         fd6_calc_gmem_cache_offsets(&info, device->gmem_size,
                                     &device->config_gmem,
                                     &device->config_sysmem);
"""

GMEM_REPLACEMENT = """      device->dev_info = info;
      device->info = &device->dev_info;

      /* WN-Turnip ECO: the per-CCU GMEM depth cache is the largest fixed carve-out
       * from GMEM (256 KB x num_ccu on a8xx_gen2 = 1.5 MB on A840). Shrinking it
       * buys tile pixels, which can be the difference between one bin and two at
       * 1080p. Env-tunable so the trade can be swept on-device.
       *
       * Applied to device->dev_info (the mutable copy) rather than the const
       * local `info`, and the offsets below are computed from that same copy so
       * the override actually takes effect.
       */
      if (fd_dev_gen(&device->dev_id) == 8) {
         /* The CCU depth cache is described two ways that MUST agree: the
          * fraction enum is what actually programs RB_CCU_CACHE_CNTL, while the
          * byte size only drives the GMEM offset arithmetic. Setting the size
          * alone would leave the hardware using a larger cache than the region
          * reserved for it, and it would scribble over tile memory. So only the
          * four representable points are accepted, and both fields move together.
          * Full depth CCU capacity on a8xx_gen2 is 256 KB per CCU.
          */
         static const struct {
            uint32_t kb;
            uint32_t fraction; /* enum a6xx_ccu_cache_size */
         } eco_depth_opts[] = {
            {  64, 2 }, /* QUARTER       */
            { 128, 1 }, /* HALF          */
            { 192, 3 }, /* THREE_QUARTER (a8xx_gen2+) */
            { 256, 0 }, /* FULL          */
         };
         int64_t eco_depth_kb =
            debug_get_num_option("TU_ECO_DEPTH_CACHE_KB", 0);
         if (eco_depth_kb != 0) {
            bool matched = false;
            for (unsigned i = 0; i < ARRAY_SIZE(eco_depth_opts); i++) {
               if ((int64_t) eco_depth_opts[i].kb != eco_depth_kb)
                  continue;
               device->dev_info.props.gmem_per_ccu_depth_cache_size =
                  eco_depth_opts[i].kb * 1024;
               device->dev_info.props.gmem_ccu_depth_cache_fraction =
                  eco_depth_opts[i].fraction;
               matched = true;
               if (TU_DEBUG(STARTUP))
                  mesa_logi("WN-ECO: depth CCU cache -> %u KB (fraction %u)",
                            eco_depth_opts[i].kb, eco_depth_opts[i].fraction);
               break;
            }
            if (!matched)
               mesa_logw("WN-ECO: TU_ECO_DEPTH_CACHE_KB=%" PRId64
                         " invalid; use 64, 128, 192 or 256. Ignoring.",
                         eco_depth_kb);
         }
      }

      device->usable_gmem_size_gmem =
         fd6_calc_gmem_cache_offsets(&device->dev_info, device->gmem_size,
                                     &device->config_gmem,
                                     &device->config_sysmem);

      if (TU_DEBUG(STARTUP)) {
         const struct fd_dev_info *ei = &device->dev_info;
         uint32_t align = 8 * ei->tile_align_w * ei->tile_align_h;
         mesa_logi("WN-ECO: gmem_size=%u usable_gmem=%u carved=%u align=%u blocks=%u",
                   device->gmem_size, device->usable_gmem_size_gmem,
                   device->gmem_size - device->usable_gmem_size_gmem, align,
                   align ? device->usable_gmem_size_gmem / align : 0);
         mesa_logi("WN-ECO: num_ccu=%u depth_cache=%u color_cache=%u tile_align=%ux%u",
                   ei->num_ccu, ei->props.gmem_per_ccu_depth_cache_size,
                   ei->props.gmem_per_ccu_color_cache_size,
                   ei->tile_align_w, ei->tile_align_h);
      }
"""


def patch_device():
    global changed_any
    content = read(DEVICE_CC)
    if content is None:
        return

    if "WN-ECO: gmem_size=" in content:
        print(f"  {DEVICE_CC}: GMEM logging + depth-cache override already present")
        return

    if GMEM_ANCHOR not in content:
        print(f"  {DEVICE_CC}: gmem setup anchor absent (upstream restructured) — skipping")
        return

    content = content.replace(GMEM_ANCHOR, GMEM_REPLACEMENT, 1)
    write(DEVICE_CC, content)
    changed_any = True
    print(f"  {DEVICE_CC}: added GMEM budget logging + TU_ECO_DEPTH_CACHE_KB override")


# ── 3: default a8xx to the adaptive bandwidth autotuner ──────────────────────

AT_ANCHOR = """      if (algo_str)
         algo_strv = algo_str;
      else if (device->instance->drirc.perf.autotune_algo)
         algo_strv = device->instance->drirc.perf.autotune_algo;
"""

AT_REPLACEMENT = """      if (algo_str)
         algo_strv = algo_str;
      else if (device->instance->drirc.perf.autotune_algo)
         algo_strv = device->instance->drirc.perf.autotune_algo;

      /* WN-Turnip ECO: upstream hardcodes prefer_sysmem for DXVK/vkd3d, which is
       * an unconditional early return that collects no metrics -- it disables
       * tiling outright. That was tuned for parts with 1-3 MB of GMEM; a8xx has
       * up to 18 MB, where the bandwidth heuristic (which picks whichever mode
       * moves less DRAM traffic) is the better default. An explicit
       * TU_AUTOTUNE_ALGO still wins, so this stays A/B-testable.
       */
      if (!algo_str && algo_strv == "prefer_sysmem" &&
          device->physical_device->info->chip == 8) {
         algo_strv = "bandwidth";
         if (TU_DEBUG(STARTUP))
            mesa_logi("WN-ECO: a8xx - overriding drirc prefer_sysmem with bandwidth");
      }
"""


# tu_autotune.cc uses os_get_option but does not pull in u_debug.h, so
# debug_get_num_option is not declared there. Add the include rather than
# hand-rolling a parser.
INC_ANCHOR = '#include "util/rand_xor.h"'
INC_REPLACEMENT = '#include "util/rand_xor.h"\n#include "util/u_debug.h"'

BW_ANCHOR = \
    "         gmem_bandwidth = (gmem_bandwidth * 11 + total_draw_call_bandwidth) / 10;"

BW_REPLACEMENT = """         /* WN-Turnip ECO: 11/10 penalises GMEM by ~10% in the bandwidth estimate.
          * Tunable via TU_ECO_GMEM_BW_NUM so the bias can be swept on-device;
          * function-local static keeps the env read off the per-renderpass path.
          */
         static const uint64_t eco_bw_num =
            (uint64_t) debug_get_num_option("TU_ECO_GMEM_BW_NUM", 11);
         gmem_bandwidth = (gmem_bandwidth * eco_bw_num + total_draw_call_bandwidth) / 10;"""


def patch_autotune():
    global changed_any
    content = read(AUTOTUNE_CC)
    if content is None:
        return

    if "WN-ECO: a8xx - overriding drirc" in content:
        print(f"  {AUTOTUNE_CC}: a8xx autotune default already present")
    elif AT_ANCHOR not in content:
        print(f"  {AUTOTUNE_CC}: autotune algo-resolution anchor absent — skipping")
    else:
        content = content.replace(AT_ANCHOR, AT_REPLACEMENT, 1)
        changed_any = True
        print(f"  {AUTOTUNE_CC}: a8xx now defaults to the bandwidth autotuner")

    # The bandwidth heuristic's GMEM estimate is scaled by 11/10, i.e. GMEM is
    # assumed ~10% more expensive than the raw calculation says. On a part with
    # 18 MB of GMEM that penalty is likely mistuned. Make the numerator tunable
    # so it can be swept on-device rather than costing a build per value.
    if "TU_ECO_GMEM_BW_NUM" in content:
        print(f"  {AUTOTUNE_CC}: gmem bandwidth numerator already tunable")
    elif BW_ANCHOR not in content:
        print(f"  {AUTOTUNE_CC}: gmem bandwidth anchor absent (or already retuned) — skipping")
    elif INC_ANCHOR not in content and '"util/u_debug.h"' not in content:
        # Without the declaration this would not compile; skip rather than break the build.
        print(f"  {AUTOTUNE_CC}: cannot place u_debug.h include — skipping bandwidth tunable")
    else:
        if '"util/u_debug.h"' not in content:
            content = content.replace(INC_ANCHOR, INC_REPLACEMENT, 1)
        content = content.replace(BW_ANCHOR, BW_REPLACEMENT, 1)
        changed_any = True
        print(f"  {AUTOTUNE_CC}: gmem bandwidth numerator now TU_ECO_GMEM_BW_NUM (default 11)")

    write(AUTOTUNE_CC, content)


if __name__ == "__main__":
    patch_device()
    patch_autotune()
    if not changed_any:
        print("apply_eco_variant.py: nothing changed (already applied or anchors absent)")
    print("apply_eco_variant.py: done")
    sys.exit(0)
