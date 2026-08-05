#!/usr/bin/env python3
"""
Request UBWC-compressed swapchain buffers on Adreno.

WHY
    Paired same-session SurfaceFlinger captures on an A840 (frame gen ON,
    upscaling OFF) show the swapchain source buffer differing by exactly one
    usage bit:

        system Adreno driver : usage 0x10000b00  12852 KiB  compressed: true
        Turnip               : usage 0x00000b00  12768 KiB  compressed: false

    12768 KiB is 1216*2688*4 exactly - linear. 12852 KiB is that plus the UBWC
    metadata plane. Every other buffer on the device carries 0x10000000; only
    Turnip's swapchain buffer lacks it, so gralloc allocates it uncompressed.

    0x10000000 is Qualcomm's private "allocate UBWC" gralloc usage bit. Mesa
    sets no vendor usage bits anywhere today - vk_android.c knows only
    GRALLOC_USAGE_HW_RENDER and GRALLOC_USAGE_HW_TEXTURE - so Turnip has never
    asked for compression and gralloc has no reason to grant it.

    Motivating symptom: GameSpace frame generation renders green under Turnip
    and correctly under the system driver. Frame interpolation reads TWO full
    frames per output frame (~26 MB/frame at 1216x2688 linear RGBA) where
    upscaling reads one; UBWC roughly halves that. UNPROVEN - this patch is the
    experiment that tests it. Independently, UBWC on the swapchain reduces DRAM
    traffic on every present regardless of GameSpace.

WHY NOT THE STANDARD ROUTE
    VK_EXT_image_compression_control is the vendor-neutral mechanism and
    vk_android.c:317 has a TODO pointing at it. It does not help here: that
    extension exists for an *application* to request compression, and
    WinNative/DXVK never will. The blob does not wait to be asked - it compresses
    by default. Matching that is a driver default, not an app-facing option.

GATING
    0x10000000 lives in AIDL BufferUsage's VENDOR_MASK (bits 28-31), so it means
    whatever each vendor decides. Setting it on non-Qualcomm hardware could mean
    something unrelated or fail allocation outright. This build only ever
    contains freedreno (-Dvulkan-drivers=freedreno), so the bit is emitted only
    from Turnip's own ANDROID_native_buffer entry points, never unconditionally.

    A better long-term gate is the *detected* gralloc vendor - the AIMapper
    backend already knows it loaded mapper.qti.so. Worth doing if this proves
    out; not worth the plumbing before we know the bit helps.

Idempotent. Three states per MAINTENANCE.md.
"""
import os
import sys

VK_ANDROID = "src/vulkan/runtime/vk_android.c"

# Qualcomm private gralloc usage: allocate UBWC-compressed.
DEFINE = """
/* WN-Turnip: Qualcomm private gralloc usage bit requesting a UBWC-compressed
 * allocation. Confirmed by observation - every buffer on an A840 carrying this
 * bit is reported "compressed: true" by SurfaceFlinger and is sized with a UBWC
 * metadata plane; the one buffer without it is exactly w*h*4 and uncompressed.
 * Lives in AIDL BufferUsage's VENDOR_MASK, so it is only ever emitted from this
 * freedreno-only build.
 */
#define WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC 0x10000000
"""

ANCHOR_DEFINE = """static VkResult
setup_gralloc0_usage(VkFormat format, VkImageUsageFlags image_usage,
                     int *out_gralloc_usage)
{"""

NEW_DEFINE = DEFINE + ANCHOR_DEFINE

# 1. gralloc0 path
ANCHOR_G0 = """   if (!gralloc_usage)
      return VK_ERROR_FORMAT_NOT_SUPPORTED;

   *out_gralloc_usage = gralloc_usage;"""

NEW_G0 = """   if (!gralloc_usage)
      return VK_ERROR_FORMAT_NOT_SUPPORTED;

   /* WN-Turnip: ask gralloc for UBWC. Without this the swapchain is allocated
    * linear, which the vendor display/video path will not consume for
    * multi-frame reads (frame interpolation). Adreno-only by construction.
    */
   gralloc_usage |= WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC;

   /* DIAGNOSTIC: the first build of this patch produced no observable change
    * in the allocated buffer (still usage 0xb00, compressed: false), so log
    * whether this path runs at all and what it asked for. Called once per
    * swapchain creation, so not spammy. Remove once the route is understood.
    */
   mesa_logi("WN-UBWC: setup_gralloc0_usage -> 0x%x (fmt %d img_usage 0x%x)",
             gralloc_usage, format, image_usage);

   *out_gralloc_usage = gralloc_usage;"""

# 2. gralloc1 path - the bit is a raw usage value, so it rides on the producer
#    side (the GPU is what writes these buffers).
ANCHOR_G1 = """   if (gralloc_usage & GRALLOC_USAGE_HW_TEXTURE)
      *grallocConsumerUsage |= GRALLOC1_CONSUMER_USAGE_GPU_TEXTURE;"""

NEW_G1 = """   if (gralloc_usage & GRALLOC_USAGE_HW_TEXTURE)
      *grallocConsumerUsage |= GRALLOC1_CONSUMER_USAGE_GPU_TEXTURE;

   /* WN-Turnip: carry the UBWC request through the gralloc1 translation. It is
    * a raw usage bit, not a gralloc1 producer/consumer flag, so it passes
    * through verbatim on the producer side.
    */
   if (gralloc_usage & WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC)
      *grallocProducerUsage |= WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC;

   /* DIAGNOSTIC: see the note in setup_gralloc0_usage. Shows which of the two
    * ANB entry points the Android loader actually calls, and the final
    * producer/consumer split handed back to it.
    */
   mesa_logi("WN-UBWC: GetSwapchainGrallocUsage2 -> producer 0x%llx consumer 0x%llx",
             (unsigned long long) *grallocProducerUsage,
             (unsigned long long) *grallocConsumerUsage);"""


def edit(content, old, new, what):
    if new in content:
        print(f"  {VK_ANDROID}: {what} already applied")
        return content, True
    if old not in content:
        print(f"  WARNING: {VK_ANDROID}: anchor absent for {what} "
              f"- upstream refactored? skipping", file=sys.stderr)
        return content, False
    print(f"  {VK_ANDROID}: {what} applied")
    return content.replace(old, new, 1), True


if not os.path.exists(VK_ANDROID):
    print(f"  WARNING: {VK_ANDROID} missing - skipping", file=sys.stderr)
    print("add_ubwc_swapchain_usage.py: done (nothing to do)")
    sys.exit(0)

with open(VK_ANDROID) as f:
    content = f.read()

content, ok_def = edit(content, ANCHOR_DEFINE, NEW_DEFINE, "usage bit define")
content, ok_g0 = edit(content, ANCHOR_G0, NEW_G0, "gralloc0 usage")
content, ok_g1 = edit(content, ANCHOR_G1, NEW_G1, "gralloc1 usage")

# The define must land or the two uses will not compile. If its anchor drifted
# but a use applied, back the whole thing out rather than ship a broken tree.
if (ok_g0 or ok_g1) and not ok_def and "WN_GRALLOC_USAGE_QCOM_ALLOC_UBWC 0x10000000" not in content:
    print("  FATAL: define anchor absent but a use applied - not writing",
          file=sys.stderr)
    sys.exit(1)

with open(VK_ANDROID, "w") as f:
    f.write(content)

print("add_ubwc_swapchain_usage.py: done")
