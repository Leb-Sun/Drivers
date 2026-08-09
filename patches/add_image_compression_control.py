#!/usr/bin/env python3
"""
Implement VK_EXT_image_compression_control in Turnip.

WHY
    This is the sanctioned way to do what add_ubwc_swapchain_usage.py does by
    hand. Android's Vulkan loader already forwards
    VK_STRUCTURE_TYPE_IMAGE_COMPRESSION_CONTROL_EXT into the AHB usage query -
    it is the *only* struct swapchain.cpp carries across from the swapchain
    pNext chain - so the platform is wired for it and Turnip is the missing
    half. The Adreno blob advertises it; Turnip advertises neither it nor the
    swapchain variant.

    With this in place an application can ask for or refuse compression and then
    find out what it actually got, instead of the driver deciding unilaterally.

SCOPE
    Base extension only. VK_EXT_image_compression_control_swapchain is a
    separate, larger job: it needs the request plumbed through the ANB/WSI path
    rather than ordinary vkCreateImage.

    Adreno UBWC is lossless, so no fixed-rate levels are advertised -
    imageCompressionFixedRateFlags is always NONE. Per the spec an application
    that asks for FIXED_RATE gets no fixed-rate compression, which is what
    reporting NONE means.

    Most of the work is already done in Mesa's common runtime:
      - vk_image.c parses VkImageCompressionControlEXT into vk_image::compr_flags
      - wsi_common.c already gates on the extension bit
      - tu_GetImageSubresourceLayout2KHR already exists as the query entry point
    What is added here is advertisement, honouring the request, and answering
    the two "what did I get" queries.

Idempotent. Three states per MAINTENANCE.md.
"""
import os
import sys

TU_DEVICE = "src/freedreno/vulkan/tu_device.cc"
TU_IMAGE = "src/freedreno/vulkan/tu_image.cc"
TU_FORMATS = "src/freedreno/vulkan/tu_formats.cc"

# ---- 1. advertise the extension (list is alphabetical) ---------------------
ANCHOR_EXT = """      .EXT_image_2d_view_of_3d = true,
      .EXT_image_drm_format_modifier = true,"""
NEW_EXT = """      .EXT_image_2d_view_of_3d = true,
      .EXT_image_compression_control = true,
      .EXT_image_drm_format_modifier = true,"""

# ---- 2. advertise the feature ---------------------------------------------
ANCHOR_FEAT = """   /* VK_EXT_image_2d_view_of_3d  */
   features->image2DViewOf3D = true;"""
NEW_FEAT = """   /* VK_EXT_image_compression_control */
   features->imageCompressionControl = true;

   /* VK_EXT_image_2d_view_of_3d  */
   features->image2DViewOf3D = true;"""

# ---- 3. honour the request when choosing UBWC ------------------------------
# Placed after force_linear_tile resolves and BEFORE the QCOM_COMPRESSED
# modifier override: an imported AHB that is already compressed cannot be
# un-compressed on request, and gralloc, not the app, owns that decision.
ANCHOR_IMG = """   if (force_linear_tile) {
      tile_mode = TILE6_LINEAR;
      ubwc_enabled = false;
   }"""
NEW_IMG = """   if (force_linear_tile) {
      tile_mode = TILE6_LINEAR;
      ubwc_enabled = false;
   }

   /* VK_EXT_image_compression_control: an explicit refusal is honoured. UBWC is
    * lossless so there is no fixed-rate level to select, and DEFAULT keeps the
    * driver's own choice. Deliberately before the QCOM_COMPRESSED override
    * below - an imported AHB that gralloc already allocated compressed cannot
    * be un-compressed here.
    */
   if (image->vk.compr_flags & VK_IMAGE_COMPRESSION_DISABLED_EXT)
      ubwc_enabled = false;"""

# ---- 4. answer "what did I get" on the image query -------------------------
ANCHOR_LAYOUT = """   pLayout->subresourceLayout.offset =
      fdl_surface_offset(layout, pSubresource->imageSubresource.mipLevel,
                         pSubresource->imageSubresource.arrayLayer);"""
NEW_LAYOUT = """   VkImageCompressionPropertiesEXT *compr_props =
      (VkImageCompressionPropertiesEXT *) vk_find_struct(
         pLayout->pNext, IMAGE_COMPRESSION_PROPERTIES_EXT);
   if (compr_props) {
      compr_props->imageCompressionFlags =
         layout->ubwc ? VK_IMAGE_COMPRESSION_DEFAULT_EXT
                      : VK_IMAGE_COMPRESSION_DISABLED_EXT;
      compr_props->imageCompressionFixedRateFlags =
         VK_IMAGE_COMPRESSION_FIXED_RATE_NONE_EXT;
   }

   pLayout->subresourceLayout.offset =
      fdl_surface_offset(layout, pSubresource->imageSubresource.mipLevel,
                         pSubresource->imageSubresource.arrayLayer);"""

# ---- 5. answer it on the physical-device format query ----------------------
# This is how an app discovers support before creating anything.
ANCHOR_FMT_IN = """      case VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_VIEW_IMAGE_FORMAT_INFO_EXT:
         image_view_info = (const VkPhysicalDeviceImageViewImageFormatInfoEXT *) s;
         break;"""
NEW_FMT_IN = """      case VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_IMAGE_VIEW_IMAGE_FORMAT_INFO_EXT:
         image_view_info = (const VkPhysicalDeviceImageViewImageFormatInfoEXT *) s;
         break;
      case VK_STRUCTURE_TYPE_IMAGE_COMPRESSION_CONTROL_EXT:
         compr_control = (const VkImageCompressionControlEXT *) s;
         break;"""

ANCHOR_FMT_OUT = """      case VK_STRUCTURE_TYPE_HOST_IMAGE_COPY_DEVICE_PERFORMANCE_QUERY_EXT:
         hic_props = (VkHostImageCopyDevicePerformanceQueryEXT *) s;
         break;"""
NEW_FMT_OUT = """      case VK_STRUCTURE_TYPE_HOST_IMAGE_COPY_DEVICE_PERFORMANCE_QUERY_EXT:
         hic_props = (VkHostImageCopyDevicePerformanceQueryEXT *) s;
         break;
      case VK_STRUCTURE_TYPE_IMAGE_COMPRESSION_PROPERTIES_EXT:
         compr_props = (VkImageCompressionPropertiesEXT *) s;
         break;"""

# declarations for the two locals used above
ANCHOR_FMT_DECL = """   result = tu_get_image_format_properties(physical_device,
      base_info, &base_props->imageFormatProperties, &format_feature_flags);
   if (result != VK_SUCCESS)
      return result;"""
NEW_FMT_DECL = """   const VkImageCompressionControlEXT *compr_control = NULL;
   VkImageCompressionPropertiesEXT *compr_props = NULL;

   result = tu_get_image_format_properties(physical_device,
      base_info, &base_props->imageFormatProperties, &format_feature_flags);
   if (result != VK_SUCCESS)
      return result;"""

# report what a would-be image gets. UBWC is lossless, so fixed-rate is never
# offered; an explicit DISABLED request is reported back as DISABLED.
ANCHOR_FMT_FILL = """   /* From the Vulkan 1.0.42 spec:
    *
    *    If handleType is 0, vkGetPhysicalDeviceImageFormatProperties2 will
    *    behave as if VkPhysicalDeviceExternalImageFormatInfo was not"""
NEW_FMT_FILL = """   if (compr_props) {
      bool disabled = compr_control &&
         (compr_control->flags & VK_IMAGE_COMPRESSION_DISABLED_EXT);
      compr_props->imageCompressionFlags =
         disabled ? VK_IMAGE_COMPRESSION_DISABLED_EXT
                  : VK_IMAGE_COMPRESSION_DEFAULT_EXT;
      compr_props->imageCompressionFixedRateFlags =
         VK_IMAGE_COMPRESSION_FIXED_RATE_NONE_EXT;
   }

   /* From the Vulkan 1.0.42 spec:
    *
    *    If handleType is 0, vkGetPhysicalDeviceImageFormatProperties2 will
    *    behave as if VkPhysicalDeviceExternalImageFormatInfo was not"""

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


edit(TU_DEVICE, ANCHOR_EXT, NEW_EXT, "advertise EXT_image_compression_control")
edit(TU_DEVICE, ANCHOR_FEAT, NEW_FEAT, "advertise imageCompressionControl feature")
edit(TU_IMAGE, ANCHOR_IMG, NEW_IMG, "honour a DISABLED request when choosing UBWC")
edit(TU_IMAGE, ANCHOR_LAYOUT, NEW_LAYOUT, "report compression on the image query")
edit(TU_FORMATS, ANCHOR_FMT_DECL, NEW_FMT_DECL, "format query: locals")
edit(TU_FORMATS, ANCHOR_FMT_IN, NEW_FMT_IN, "format query: read the request")
edit(TU_FORMATS, ANCHOR_FMT_OUT, NEW_FMT_OUT, "format query: find the output struct")
edit(TU_FORMATS, ANCHOR_FMT_FILL, NEW_FMT_FILL, "format query: answer it")

if failed:
    print("  FATAL: a required anchor was missing", file=sys.stderr)
    sys.exit(1)

print("add_image_compression_control.py: done")
