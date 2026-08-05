#!/usr/bin/env python3
"""
Request UBWC-compressed swapchain buffers on Adreno.

WHERE THE VALUE ACTUALLY COMES FROM
    Android 16's Vulkan loader does NOT use vkGetSwapchainGrallocUsageXANDROID
    in the normal path. frameworks/native/vulkan/libvulkan/swapchain.cpp:1450+
    does this instead:

        VkAndroidHardwareBufferUsageANDROID ahb_usage;
        image_format_properties.pNext = &ahb_usage;
        GetPhysicalDeviceImageFormatProperties2(pdev, &image_format_info,
                                                &image_format_properties);
        ...
        *producer_usage = ahb_usage.androidHardwareBufferUsage;
        return VK_SUCCESS;                    <-- returns BEFORE the
                                                  GetSwapchainGrallocUsageX chain

    That chain (usage/2/3/4) is a legacy fallback for old drivers and is never
    reached here. An earlier version of this patch targeted it and had no
    observable effect, which is exactly why.

    The live path for Turnip is:
        tu_formats.cc:846
          -> vk_android_get_ahb_image_properties()   vk_android.c:1181
            -> vk_image_usage_to_ahb_usage()         vk_android.c:805
              -> ahb_usage->androidHardwareBufferUsage

    vk_image_usage_to_ahb_usage() maps Vulkan image usage to AHardwareBuffer
    usage with no vendor bits at all. WinNative asks for COLOR_ATTACHMENT ->
    GPU_FRAMEBUFFER (0x200); SurfaceFlinger's consumer side contributes
    HW_COMPOSER|HW_TEXTURE (0x900); total 0xb00 - exactly what the captures show.
    The blob returns the same plus 0x10000000 and gets 0x10000b00 + UBWC.

    Upstream knows this is incomplete. The same function carries:
      "XXX We need a better gralloc private query to forward the mutable bit
       along with the format list for a private vendor usage bit, and leave the
       decision to gralloc."

GATING
    0x10000000 is in AIDL BufferUsage's VENDOR_MASK (bits 28-31) and means
    whatever each vendor decides. This build contains only freedreno
    (-Dvulkan-drivers=freedreno), so the bit can only ever be emitted by Turnip.

    Additionally skipped whenever any CPU usage bit is set: gralloc cannot give
    a CPU-mappable UBWC buffer, and vk_image_usage_to_ahb_usage() deliberately
    sets CPU_WRITE_RARELY for MUTABLE_FORMAT images specifically to force LINEAR
    (see mesa d02b2515). Asking for both would be contradictory.

Idempotent. Three states per MAINTENANCE.md.
"""
import os
import sys

VK_ANDROID = "src/vulkan/runtime/vk_android.c"

DEFINE = """
/* WN-Turnip: Qualcomm private gralloc/AHardwareBuffer usage bit requesting a
 * UBWC-compressed allocation. Confirmed by observation - every buffer on an
 * A840 carrying this bit is reported "compressed: true" by SurfaceFlinger and
 * is sized with a UBWC metadata plane; the one buffer without it is exactly
 * w*h*4 and uncompressed. Lives in AIDL BufferUsage's VENDOR_MASK, so it is
 * only ever emitted from this freedreno-only build.
 */
#define WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC 0x10000000ull

"""

ANCHOR_DEFINE = """/* Construct ahw usage mask from image usage bits, see
 * 'AHardwareBuffer Usage Equivalence' in Vulkan spec.
 */
uint64_t
vk_image_usage_to_ahb_usage("""

NEW_DEFINE = DEFINE + ANCHOR_DEFINE

# CRITICAL SCOPING - do NOT move this into vk_image_usage_to_ahb_usage().
#
# That function serves two callers with opposite needs:
#   (a) ALLOCATION - what usage should a new swapchain buffer be created with.
#       UBWC is wanted here.
#   (b) REQUIREMENT - what usage an image needs, used when validating an IMPORT
#       of an already-allocated AHardwareBuffer. Demanding UBWC here rejects
#       every buffer that does not have it.
#
# A build that added the bit inside the function produced 448
# "nativeImportAhbToVulkan failed" errors and "Swapchain re-create failed",
# giving a black screen: WinNative's X-server images are allocated in
# gpu_image.c with CPU_READ_OFTEN|CPU_WRITE_OFTEN (logged as usage=0x332) and
# are therefore linear by necessity. A CPU-bit guard inside the function cannot
# catch this - it only sees Vulkan-derived usage, which never carries CPU bits.
#
# VkAndroidHardwareBufferUsageANDROID chained on the OUTPUT is what distinguishes
# case (a): it means "tell me what to allocate with". Import validation never
# chains it. So the bit goes here, at that call site only.
ANCHOR_AHB = """      ahb_usage->androidHardwareBufferUsage =
         vk_image_usage_to_ahb_usage(image_flags, image_usage);"""

NEW_AHB = """      uint64_t wn_ahb_usage =
         vk_image_usage_to_ahb_usage(image_flags, image_usage);

      /* WN-Turnip: ask gralloc for UBWC. Reaching this block means the caller
       * chained VkAndroidHardwareBufferUsageANDROID on the output, i.e. it is
       * asking what to ALLOCATE with - which is the Android loader sizing up a
       * swapchain buffer. Import-validation paths never chain it, so they keep
       * the unmodified requirement and can still accept linear buffers.
       *
       * Skipped when CPU access is implied: gralloc cannot hand back a
       * CPU-mappable UBWC buffer, and the MUTABLE_FORMAT case deliberately sets
       * CPU_WRITE_RARELY to force LINEAR (mesa d02b2515).
       */
      if (!(wn_ahb_usage & (AHARDWAREBUFFER_USAGE_CPU_READ_MASK |
                            AHARDWAREBUFFER_USAGE_CPU_WRITE_MASK)))
         wn_ahb_usage |= WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC;

      mesa_logi("WN-AHB: alloc query -> 0x%" PRIx64 " (create 0x%x usage 0x%x)",
                wn_ahb_usage, (unsigned) image_flags, (unsigned) image_usage);

      ahb_usage->androidHardwareBufferUsage = wn_ahb_usage;"""

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


edit(VK_ANDROID, ANCHOR_DEFINE, NEW_DEFINE, "usage bit define")
edit(VK_ANDROID, ANCHOR_AHB, NEW_AHB, "AHB usage (the live loader path)")

if failed:
    print("  FATAL: a required anchor was missing", file=sys.stderr)
    sys.exit(1)

print("add_ubwc_swapchain_usage.py: done")
