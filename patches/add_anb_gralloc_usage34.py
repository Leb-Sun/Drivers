#!/usr/bin/env python3
"""
Implement VK_ANDROID_native_buffer gralloc-usage v3 and v4, and advertise spec 10.

WHY
    Mesa's ANB support is stuck at spec 8. It dispatches only
    vkGetSwapchainGrallocUsage{,2}ANDROID, and an instrumented build proved that
    on Android 16 *neither is ever called*: the loader will not use spec-8
    gralloc-usage queries. Turnip is therefore never asked what usage its
    swapchain buffers need, and the loader substitutes a default -
    0xb00 (HW_RENDER|HW_TEXTURE|HW_COMPOSER), which gralloc allocates LINEAR.

    The Qualcomm blob dispatches usage1/2/3/4 and is asked, so its swapchain
    buffers come back 0x10000b00 and UBWC-compressed. Same app, same GameSpace,
    same everything else - only the driver differs.

    Spec history (include/vulkan/vk_android_native_buffer.h):
        9  - adds usage3, deprecates usage/usage2
        10 - fixes a bug introduced in 9, moves to usage4
        11 - deprecates ALL grallocusage, passes AHardwareBuffer* instead

    We target spec 10 deliberately. Advertising 11 would mean the loader stops
    asking for usage entirely and hands us an AHardwareBuffer*, which Turnip
    does not consume - i.e. straight back to a loader default, plus an unmet
    contract.

SAFE TO BUMP?
    Verified: Mesa reads only `handle`, `format` and `stride` from
    VkNativeBufferANDROID. It never reads `usage`, `usage2`, `usage3` or `ahb`,
    so no version-gated struct field is load-bearing. The v9/v10 deltas concern
    only which usage query the loader calls.

SCOPE
    This patch is vendor-neutral: it answers the loader with the same usage Mesa
    already computes. Requesting UBWC is a separate, Adreno-only patch
    (add_ubwc_swapchain_usage.py) deliberately kept apart so this one stays
    upstreamable.

Idempotent. Three states per MAINTENANCE.md.
"""
import os
import sys

VK_ANDROID = "src/vulkan/runtime/vk_android.c"
VK_XML = "src/vulkan/registry/vk.xml"

state = {"ok": True}


def edit(path, old, new, what, required=True):
    if not os.path.exists(path):
        print(f"  WARNING: {path} missing - skipping {what}", file=sys.stderr)
        state["ok"] = False
        return
    with open(path) as f:
        content = f.read()

    if new in content:
        print(f"  {path}: {what} already applied")
        return
    if old not in content:
        print(f"  WARNING: {path}: anchor absent for {what} "
              f"- upstream refactored? skipping", file=sys.stderr)
        if required:
            state["ok"] = False
        return

    with open(path, "w") as f:
        f.write(content.replace(old, new, 1))
    print(f"  {path}: {what} applied")


# --- 1. the two new entry points --------------------------------------------
# Anchored on the end of the existing usage2 implementation, which is the
# natural neighbour and the least likely anchor to drift independently.
ANCHOR_IMPL = """   /* for front buffer rendering */
   if (swapchainImageUsage & VK_SWAPCHAIN_IMAGE_USAGE_SHARED_BIT_ANDROID)
      *grallocProducerUsage |= vk_android_get_front_buffer_usage();

   return VK_SUCCESS;
}

VkResult
vk_android_get_ahb_layout("""

NEW_IMPL = """   /* for front buffer rendering */
   if (swapchainImageUsage & VK_SWAPCHAIN_IMAGE_USAGE_SHARED_BIT_ANDROID)
      *grallocProducerUsage |= vk_android_get_front_buffer_usage();

   return VK_SUCCESS;
}

/* ADDED in ANB SPEC_VERSION 9. The loader stops calling the v1/v2 queries once
 * it is talking to a driver that advertises 9+, so without these the driver is
 * simply never asked and the loader substitutes its own default usage.
 */
VKAPI_ATTR VkResult VKAPI_CALL
vk_common_GetSwapchainGrallocUsage3ANDROID(
   VkDevice device, const VkGrallocUsageInfoANDROID *grallocUsageInfo,
   uint64_t *grallocUsage)
{
   int gralloc_usage = 0;
   VkResult result = setup_gralloc0_usage(grallocUsageInfo->format,
                                          grallocUsageInfo->imageUsage,
                                          &gralloc_usage);
   if (result != VK_SUCCESS)
      return result;

   *grallocUsage = (uint64_t) gralloc_usage;

   mesa_logi("WN-ANB: GetSwapchainGrallocUsage3 -> 0x%" PRIx64,
             (uint64_t) *grallocUsage);

   return VK_SUCCESS;
}

/* ADDED in ANB SPEC_VERSION 10. Same as v3 plus swapchainImageUsage, which is
 * only used for the shared/front-buffer case the v2 path already handled.
 */
VKAPI_ATTR VkResult VKAPI_CALL
vk_common_GetSwapchainGrallocUsage4ANDROID(
   VkDevice device, const VkGrallocUsageInfo2ANDROID *grallocUsageInfo,
   uint64_t *grallocUsage)
{
   int gralloc_usage = 0;
   VkResult result = setup_gralloc0_usage(grallocUsageInfo->format,
                                          grallocUsageInfo->imageUsage,
                                          &gralloc_usage);
   if (result != VK_SUCCESS)
      return result;

   uint64_t usage = (uint64_t) gralloc_usage;

   if (grallocUsageInfo->swapchainImageUsage &
       VK_SWAPCHAIN_IMAGE_USAGE_SHARED_BIT_ANDROID)
      usage |= vk_android_get_front_buffer_usage();

   *grallocUsage = usage;

   mesa_logi("WN-ANB: GetSwapchainGrallocUsage4 -> 0x%" PRIx64,
             (uint64_t) *grallocUsage);

   return VK_SUCCESS;
}

VkResult
vk_android_get_ahb_layout("""

# --- 2. inttypes for PRIx64 --------------------------------------------------
edit(
    VK_ANDROID,
    '#include "util/log.h"',
    '#include <inttypes.h>\n\n#include "util/log.h"',
    "inttypes include",
)

edit(VK_ANDROID, ANCHOR_IMPL, NEW_IMPL, "usage3 + usage4 implementations")

# --- 3a. command DEFINITIONS in the <commands> section -----------------------
# Without these the generator emits no prototype and the build fails with
# -Werror,-Wmissing-prototypes. A <command name="..."/> reference in the
# extension block is not sufficient on its own.
edit(
    VK_XML,
    """            <param><type>uint64_t</type>* <name>grallocProducerUsage</name></param>
        </command>""",
    """            <param><type>uint64_t</type>* <name>grallocProducerUsage</name></param>
        </command>
        <command>
            <proto><type>VkResult</type> <name>vkGetSwapchainGrallocUsage3ANDROID</name></proto>
            <param><type>VkDevice</type> <name>device</name></param>
            <param>const <type>VkGrallocUsageInfoANDROID</type>* <name>grallocUsageInfo</name></param>
            <param><type>uint64_t</type>* <name>grallocUsage</name></param>
        </command>
        <command>
            <proto><type>VkResult</type> <name>vkGetSwapchainGrallocUsage4ANDROID</name></proto>
            <param><type>VkDevice</type> <name>device</name></param>
            <param>const <type>VkGrallocUsageInfo2ANDROID</type>* <name>grallocUsageInfo</name></param>
            <param><type>uint64_t</type>* <name>grallocUsage</name></param>
        </command>""",
    "ANB command definitions",
)

# --- 3b. dispatch: add the commands to the ANB extension block ---------------
edit(
    VK_XML,
    '                <command name="vkGetSwapchainGrallocUsage2ANDROID" />',
    '                <command name="vkGetSwapchainGrallocUsage2ANDROID" />\n'
    '                <command name="vkGetSwapchainGrallocUsage3ANDROID" />\n'
    '                <command name="vkGetSwapchainGrallocUsage4ANDROID" />',
    "ANB dispatch entries",
)

# --- 4. advertise spec 10 ----------------------------------------------------
# Deliberately NOT 11: at 11 gralloc usage is deprecated outright and the loader
# hands over an AHardwareBuffer* instead of asking, which Turnip does not consume.
edit(
    VK_XML,
    '<enum value="8" name="VK_ANDROID_NATIVE_BUFFER_SPEC_VERSION" />',
    '<enum value="10" name="VK_ANDROID_NATIVE_BUFFER_SPEC_VERSION" />',
    "ANB spec version 8 -> 10",
)

if not state["ok"]:
    print("  FATAL: a required anchor was missing; tree may be inconsistent",
          file=sys.stderr)
    sys.exit(1)

print("add_anb_gralloc_usage34.py: done")
