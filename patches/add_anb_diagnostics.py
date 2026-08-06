#!/usr/bin/env python3
"""
Instrument the ANDROID_native_buffer swapchain-image path.

WHY
    With the UBWC usage patch active, Turnip reports exactly what both working
    Adreno drivers report (`0x10000200`, measured by WinNative's AHB-USAGE-PROBE)
    and `vkCreateSwapchainKHR` still fails. So the fault is downstream: Turnip
    cannot consume the UBWC buffer the loader allocates in response.

    That code path has never executed before - Turnip's swapchain buffers have
    always been linear, so `vk_android_get_anb_layout()` has only ever returned
    DRM_FORMAT_MOD_LINEAR here.

    This logs every stage so the next black screen names its own cause instead of
    producing another theory from a symptom.

WHAT IT LOGS
    WN-ANB-LAYOUT   what u_gralloc (the AIMapper backend) reports for the buffer
                    handed over by the loader: drm_fourcc, modifier, plane count,
                    per-plane offset/stride. A modifier of 0x0500000000000001 is
                    QCOM_COMPRESSED (UBWC); 0x0 is LINEAR.
    WN-ANB-INIT     the VkResult of tu_image_init() for that image, plus the
                    total size Turnip computed for its own layout. A size larger
                    than the allocation is the classic UBWC-mismatch failure.
    WN-ANB-BIND     the VkResult of vk_android_import_anb_memory().

Idempotent. Three states per MAINTENANCE.md. Diagnostic only - no behaviour
change; remove once the failure is understood.
"""
import os
import sys

VK_ANDROID = "src/vulkan/runtime/vk_android.c"
TU_IMAGE = "src/freedreno/vulkan/tu_image.cc"

failed = False


def edit(path, old, new, what):
    global failed
    if not os.path.exists(path):
        print(f"  WARNING: {path} missing - skipping {what}", file=sys.stderr)
        failed = True
        return
    with open(path) as f:
        content = f.read()
    if new in content:
        print(f"  {path}: {what} already applied")
        return
    if old not in content:
        print(f"  WARNING: {path}: anchor absent for {what} "
              f"- upstream refactored? skipping", file=sys.stderr)
        failed = True
        return
    with open(path, "w") as f:
        f.write(content.replace(old, new, 1))
    print(f"  {path}: {what} applied")


# --- 1. what u_gralloc reports for the loader's buffer -----------------------
edit(
    VK_ANDROID,
    """   return vk_gralloc_to_drm_explicit_layout(&gr_handle, out,
                                            out_layouts, max_planes);
}""",
    """   VkResult wn_r = vk_gralloc_to_drm_explicit_layout(&gr_handle, out,
                                                     out_layouts, max_planes);

   /* WN diagnostic: this is the first place Turnip sees what the loader
    * actually allocated. modifier 0x0500000000000001 = QCOM_COMPRESSED (UBWC),
    * 0x0 = LINEAR.
    */
   if (wn_r == VK_SUCCESS) {
      mesa_logi("WN-ANB-LAYOUT: result=%d modifier=0x%" PRIx64 " planes=%u "
                "hal_fmt=%d stride=%d",
                (int) wn_r, (uint64_t) out->drmFormatModifier,
                out->drmFormatModifierPlaneCount, gr_handle.hal_format,
                gr_handle.pixel_stride);
      for (uint32_t wn_i = 0; wn_i < out->drmFormatModifierPlaneCount &&
                              wn_i < (uint32_t) max_planes; wn_i++) {
         mesa_logi("WN-ANB-LAYOUT:   plane[%u] offset=%" PRIu64
                   " rowPitch=%" PRIu64 " size=%" PRIu64,
                   wn_i, (uint64_t) out_layouts[wn_i].offset,
                   (uint64_t) out_layouts[wn_i].rowPitch,
                   (uint64_t) out_layouts[wn_i].size);
      }
   } else {
      mesa_logw("WN-ANB-LAYOUT: FAILED result=%d hal_fmt=%d stride=%d",
                (int) wn_r, gr_handle.hal_format, gr_handle.pixel_stride);
   }

   return wn_r;
}""",
    "anb layout logging",
)

edit(
    VK_ANDROID,
    '#include <inttypes.h>',
    '#include <inttypes.h>',
    "inttypes include (already added by the usage34 patch)",
)

# --- 2. tu_image_init result + computed size, and the memory import ----------
edit(
    TU_IMAGE,
    """   result = TU_CALLX(dev, tu_image_init)(
      dev, img, img->vk.android_deferred_create_info,
      eci.drmFormatModifier, a_plane_layouts, TU_IMAGE_ID_ASSIGN);
   if (result != VK_SUCCESS)
      return result;

   result = vk_android_import_anb_memory(&dev->vk, &img->vk, anb,
                                         &dev->vk.alloc);
   if (result != VK_SUCCESS)
      return result;""",
    """   result = TU_CALLX(dev, tu_image_init)(
      dev, img, img->vk.android_deferred_create_info,
      eci.drmFormatModifier, a_plane_layouts, TU_IMAGE_ID_ASSIGN);

   /* WN diagnostic: a total_size larger than the gralloc allocation is the
    * classic UBWC layout mismatch. Log before bailing so a failure is visible.
    */
   mesa_logi("WN-ANB-INIT: tu_image_init result=%d modifier=0x%" PRIx64
             " total_size=%" PRIu64,
             (int) result, (uint64_t) eci.drmFormatModifier,
             (uint64_t) img->total_size);

   if (result != VK_SUCCESS)
      return result;

   result = vk_android_import_anb_memory(&dev->vk, &img->vk, anb,
                                         &dev->vk.alloc);

   mesa_logi("WN-ANB-BIND: import_anb_memory result=%d", (int) result);

   if (result != VK_SUCCESS)
      return result;""",
    "tu_image_init + bind logging",
)

# tu_image.cc needs inttypes for PRIx64/PRIu64
edit(
    TU_IMAGE,
    '#include "tu_image.h"',
    '#include <inttypes.h>\n\n#include "util/log.h"\n#include "tu_image.h"',
    "tu_image.cc inttypes + log includes",
)

if failed:
    print("  FATAL: a required anchor was missing", file=sys.stderr)
    sys.exit(1)

print("add_anb_diagnostics.py: done")
