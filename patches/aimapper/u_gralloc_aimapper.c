/*
 * Mesa 3-D graphics library
 * SPDX-License-Identifier: MIT
 */

/*
 * IMapper5 stable-C backend, reached via the SP-HAL loader.
 *
 * The existing IMapper backends (u_gralloc_imapper{4,5}_api.cpp) go through
 * libui's C++ GraphicBufferMapper, so they need dep_android_ui / dep_android_mapper4.
 * A Mesa built with -Dandroid-stub=true never even probes for those (meson.build
 * pins them to null_dep), so a driver loaded into an app process — adrenotools,
 * emulators, Winlator-likes — always lands on u_gralloc_fallback.c and loses
 * YCbCr support, the real modifier, and the dataspace.
 *
 * libui itself reaches the vendor mapper through AIMapper_loadIMapper(), a plain
 * C entry point that android_load_sphal_library() can resolve from a non-vendor
 * process. This backend does that directly, so it needs only dlopen/dlsym.
 */

#include <dirent.h>
#include <dlfcn.h>
#include <errno.h>
#include <inttypes.h>
#include <stdalign.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "drm-uapi/drm_fourcc.h"
#include "util/log.h"
#include "util/u_memory.h"

#include "u_gralloc_internal.h"

/* ---------------------------------------------------------------------------
 * AIMapper ABI. Mirrors AOSP IMapper.h; member order is load-bearing, so the
 * unused tail is kept (as void *) rather than truncated.
 * --------------------------------------------------------------------------- */

typedef struct AIMapperV5 {
   void *importBuffer;
   void *freeBuffer;
   void *getTransportSize;
   void *lock;
   void *unlock;
   void *flushLockedBuffer;
   void *rereadLockedBuffer;
   void *getMetadata;
   int32_t (*getStandardMetadata)(const native_handle_t *buffer,
                                  int64_t standardMetadataType, void *destBuffer,
                                  size_t destBufferSize);
   void *setMetadata;
   void *setStandardMetadata;
   void *listSupportedMetadataTypes;
   void *dumpBuffer;
   void *dumpAllBuffers;
   void *getReservedRegion;
} AIMapperV5;

typedef struct AIMapper {
   alignas(alignof(max_align_t)) uint32_t version;
   AIMapperV5 v5;
} AIMapper;

#define AIMAPPER_VERSION_5 5

/* StandardMetadataType.aidl */
#define SMT_PIXEL_FORMAT_FOURCC  7
#define SMT_PIXEL_FORMAT_MODIFIER 8
#define SMT_CHROMA_SITING        14
#define SMT_PLANE_LAYOUTS        15
#define SMT_DATASPACE            17

/* Dataspace bit fields */
#define DS_STANDARD_MASK  (63u << 16)
#define DS_STANDARD_BT709 (1u << 16)
#define DS_STANDARD_BT2020        (6u << 16)
#define DS_STANDARD_BT2020_CL     (7u << 16)
#define DS_RANGE_MASK    (7u << 27)
#define DS_RANGE_FULL    (1u << 27)

/* ChromaSiting.aidl */
#define CHROMA_SITING_COSITED_HORIZONTAL 3
#define CHROMA_SITING_COSITED_VERTICAL   4
#define CHROMA_SITING_COSITED_BOTH       5

struct aimapper_gralloc {
   struct u_gralloc base;
   void *mapper_lib;
   AIMapper *mapper;
};

/* ---------------------------------------------------------------------------
 * Metadata decoding.
 *
 * Every value is framed: an int64 name length, the type-name bytes, an int64
 * type id, then the payload. Nothing is padded, so the payload is not naturally
 * aligned — read through memcpy only.
 * --------------------------------------------------------------------------- */

struct md_reader {
   const uint8_t *p;
   const uint8_t *end;
   bool ok;
};

static bool
md_take(struct md_reader *r, void *dst, size_t n)
{
   if (!r->ok || (size_t)(r->end - r->p) < n) {
      r->ok = false;
      return false;
   }
   if (dst)
      memcpy(dst, r->p, n);
   r->p += n;
   return true;
}

static int64_t
md_i64(struct md_reader *r)
{
   int64_t v = 0;
   md_take(r, &v, sizeof(v));
   return v;
}

/* Skips a length-prefixed string. */
static void
md_skip_str(struct md_reader *r)
{
   int64_t len = md_i64(r);
   if (len < 0 || len > (r->end - r->p)) {
      r->ok = false;
      return;
   }
   md_take(r, NULL, (size_t)len);
}

/* Consumes the [name][type] frame and checks the type id matches. */
static bool
md_header(struct md_reader *r, int64_t expect_type)
{
   md_skip_str(r);
   return r->ok && md_i64(r) == expect_type && r->ok;
}

/* Fetches one metadata value into buf. Returns payload byte count, or < 0. */
static int32_t
md_fetch(struct aimapper_gralloc *gr, const native_handle_t *handle,
         int64_t type, void *buf, size_t buf_size)
{
   int32_t n = gr->mapper->v5.getStandardMetadata(handle, type, buf, buf_size);
   if (n < 0 || (size_t)n > buf_size)
      return -1;
   return n;
}

/* Scalars are small; one stack buffer covers header + value comfortably. */
static bool
md_scalar(struct aimapper_gralloc *gr, const native_handle_t *handle,
          int64_t type, void *out, size_t out_size)
{
   uint8_t buf[160];
   int32_t n = md_fetch(gr, handle, type, buf, sizeof(buf));
   if (n < 0)
      return false;

   struct md_reader r = {buf, buf + n, true};
   if (!md_header(&r, type))
      return false;

   return md_take(&r, out, out_size);
}

/* ---------------------------------------------------------------------------
 * ops
 * --------------------------------------------------------------------------- */

static int
aimapper_get_buffer_basic_info(struct u_gralloc *gralloc,
                               struct u_gralloc_buffer_handle *hnd,
                               struct u_gralloc_buffer_basic_info *out)
{
   struct aimapper_gralloc *gr = (struct aimapper_gralloc *)gralloc;

   if (!hnd->handle) {
      mesa_logw("aimapper: FAIL null handle");
      return -EINVAL;
   }

   uint32_t fourcc = 0;
   if (!md_scalar(gr, hnd->handle, SMT_PIXEL_FORMAT_FOURCC, &fourcc,
                  sizeof(fourcc))) {
      mesa_logw("aimapper: PIXEL_FORMAT_FOURCC unavailable");
      return -EINVAL;
   }
   out->drm_fourcc = fourcc;

   uint64_t modifier = 0;
   if (md_scalar(gr, hnd->handle, SMT_PIXEL_FORMAT_MODIFIER, &modifier,
                 sizeof(modifier)))
      out->modifier = modifier;
   else
      out->modifier = DRM_FORMAT_MOD_INVALID;

   /* Plane layouts: [numPlanes] then per plane [numComponents]
    * [component: name-string, int64 offsetInBits, int64 sizeInBits] * n
    * followed by 8 int64 plane fields, of which we want offsetInBytes and
    * strideInBytes.
    */
   int32_t need = gr->mapper->v5.getStandardMetadata(hnd->handle,
                                                     SMT_PLANE_LAYOUTS, NULL, 0);
   if (need <= 0) {
      mesa_logw("aimapper: PLANE_LAYOUTS unavailable (%d)", need);
      return -EINVAL;
   }

   /* Everything the gotos skip past is declared up front, so no jump crosses an
    * initialisation.
    */
   uint8_t *buf = NULL;
   int ret = -EINVAL;
   int32_t got = 0;
   int64_t num_planes = 0;
   struct md_reader r = {NULL, NULL, false};

   buf = MALLOC(need);
   if (!buf) {
      mesa_logw("aimapper: FAIL malloc %d", need);
      return -ENOMEM;
   }

   got = md_fetch(gr, hnd->handle, SMT_PLANE_LAYOUTS, buf, need);
   if (got < 0) {
      mesa_logw("aimapper: FAIL PLANE_LAYOUTS fetch (need=%d got=%d)", need, got);
      goto out_free;
   }

   r = (struct md_reader){buf, buf + got, true};
   if (!md_header(&r, SMT_PLANE_LAYOUTS)) {
      mesa_logw("aimapper: FAIL PLANE_LAYOUTS header framing (%d bytes)", got);
      goto out_free;
   }

   num_planes = md_i64(&r);
   if (!r.ok || num_planes <= 0 || num_planes > 4) {
      mesa_logw("aimapper: implausible plane count %" PRId64, num_planes);
      goto out_free;
   }

   for (int64_t i = 0; i < num_planes && r.ok; i++) {
      int64_t num_components = md_i64(&r);
      if (!r.ok || num_components < 0 || num_components > 16) {
         mesa_logw("aimapper: FAIL plane %" PRId64 " component count %" PRId64
                   " (ok=%d)", i, num_components, (int) r.ok);
         goto out_free;
      }

      for (int64_t j = 0; j < num_components && r.ok; j++) {
         md_skip_str(&r);   /* component type name */
         md_i64(&r);        /* component type value */
         md_i64(&r);        /* offsetInBits */
         md_i64(&r);        /* sizeInBits */
      }

      int64_t offset_bytes = md_i64(&r);
      md_i64(&r);                            /* sampleIncrementInBits */
      int64_t stride_bytes = md_i64(&r);
      md_i64(&r);                            /* widthInSamples */
      md_i64(&r);                            /* heightInSamples */
      md_i64(&r);                            /* totalSizeInBytes */
      md_i64(&r);                            /* horizontalSubsampling */
      md_i64(&r);                            /* verticalSubsampling */

      if (!r.ok) {
         mesa_logw("aimapper: FAIL plane %" PRId64 " field decode ran short", i);
         goto out_free;
      }

      out->offsets[i] = (int)offset_bytes;
      out->strides[i] = (int)stride_bytes;
      mesa_logi("aimapper: plane[%" PRId64 "] offset=%" PRId64
                " stride=%" PRId64, i, offset_bytes, stride_bytes);
   }

   out->num_planes = (int)num_planes;

   /* Plane layouts carry no fds. Mirror the other backends: one fd shared by
    * every plane, or one fd per plane when the handle provides them.
    */
   if (hnd->handle->numFds == 0) {
      mesa_logw("aimapper: FAIL handle has no fds");
      goto out_free;
   }

   if (hnd->handle->numFds >= num_planes) {
      for (int64_t i = 0; i < num_planes; i++)
         out->fds[i] = hnd->handle->data[i];
   } else {
      for (int64_t i = 0; i < num_planes; i++)
         out->fds[i] = hnd->handle->data[0];
   }

   ret = 0;
   mesa_logi("aimapper: OK fourcc=0x%x modifier=0x%" PRIx64 " planes=%d "
             "hal_fmt=%d pixel_stride=%d",
             out->drm_fourcc, out->modifier, out->num_planes,
             hnd->hal_format, hnd->pixel_stride);

out_free:
   FREE(buf);
   return ret;
}

static int
aimapper_get_buffer_color_info(struct u_gralloc *gralloc,
                               struct u_gralloc_buffer_handle *hnd,
                               struct u_gralloc_buffer_color_info *out)
{
   struct aimapper_gralloc *gr = (struct aimapper_gralloc *)gralloc;

   if (!hnd->handle)
      return -EINVAL;

   /* Defaults match u_gralloc.c's when a backend has no color op at all. */
   out->yuv_color_space = __DRI_YUV_COLOR_SPACE_ITU_REC601;
   out->sample_range = __DRI_YUV_NARROW_RANGE;
   out->horizontal_siting = __DRI_YUV_CHROMA_SITING_0_5;
   out->vertical_siting = __DRI_YUV_CHROMA_SITING_0_5;

   uint32_t dataspace = 0;
   if (md_scalar(gr, hnd->handle, SMT_DATASPACE, &dataspace,
                 sizeof(dataspace))) {
      switch (dataspace & DS_STANDARD_MASK) {
      case DS_STANDARD_BT709:
         out->yuv_color_space = __DRI_YUV_COLOR_SPACE_ITU_REC709;
         break;
      case DS_STANDARD_BT2020:
      case DS_STANDARD_BT2020_CL:
         out->yuv_color_space = __DRI_YUV_COLOR_SPACE_ITU_REC2020;
         break;
      default:
         break;
      }

      if ((dataspace & DS_RANGE_MASK) == DS_RANGE_FULL)
         out->sample_range = __DRI_YUV_FULL_RANGE;
   }

   /* ChromaSiting is an ExtendableType: a name string then an int64 value. */
   uint8_t buf[160];
   int32_t n = md_fetch(gr, hnd->handle, SMT_CHROMA_SITING, buf, sizeof(buf));
   if (n > 0) {
      struct md_reader r = {buf, buf + n, true};
      if (md_header(&r, SMT_CHROMA_SITING)) {
         md_skip_str(&r); /* ExtendableType.name */
         int64_t siting = md_i64(&r);
         if (r.ok) {
            switch (siting) {
            case CHROMA_SITING_COSITED_HORIZONTAL:
               out->horizontal_siting = __DRI_YUV_CHROMA_SITING_0;
               break;
            case CHROMA_SITING_COSITED_VERTICAL:
               out->vertical_siting = __DRI_YUV_CHROMA_SITING_0;
               break;
            case CHROMA_SITING_COSITED_BOTH:
               out->horizontal_siting = __DRI_YUV_CHROMA_SITING_0;
               out->vertical_siting = __DRI_YUV_CHROMA_SITING_0;
               break;
            default:
               break;
            }
         }
      }
   }

   return 0;
}

static int
destroy(struct u_gralloc *gralloc)
{
   struct aimapper_gralloc *gr = (struct aimapper_gralloc *)gralloc;

   /* The mapper is owned by the vendor library; only the handle is ours.
    * Leave it loaded — unloading an SP-HAL under a live driver is not worth
    * the risk for a process-lifetime singleton.
    */
   FREE(gr);

   return 0;
}

/* ---------------------------------------------------------------------------
 * create
 * --------------------------------------------------------------------------- */

typedef void *(*sphal_load_fn)(const char *name, int flag);
typedef int32_t (*load_imapper_fn)(AIMapper **outImplementation);

/* The platform asks IAllocator for the mapper suffix. Binder is far too heavy a
 * dependency here, so enumerate the vendor HAL directory instead.
 */
static void *
load_vendor_mapper(sphal_load_fn sphal, load_imapper_fn *out_load)
{
   static const char *const dirs[] = {"/vendor/lib64/hw", "/vendor/lib/hw"};

   for (unsigned d = 0; d < ARRAY_SIZE(dirs); d++) {
      DIR *dir = opendir(dirs[d]);
      if (!dir)
         continue;

      struct dirent *ent;
      while ((ent = readdir(dir))) {
         if (strncmp(ent->d_name, "mapper.", 7) != 0)
            continue;
         size_t len = strlen(ent->d_name);
         if (len < 4 || strcmp(ent->d_name + len - 3, ".so") != 0)
            continue;

         void *lib = sphal(ent->d_name, RTLD_NOW);
         if (!lib)
            continue;

         load_imapper_fn fn =
            (load_imapper_fn)dlsym(lib, "AIMapper_loadIMapper");
         if (fn) {
            closedir(dir);
            *out_load = fn;
            return lib;
         }
      }
      closedir(dir);
   }

   return NULL;
}

struct u_gralloc *
u_gralloc_aimapper_create(void)
{
   /* libvndksupport is the sanctioned route for a non-vendor process to load a
    * same-process vendor HAL; the Android Vulkan loader uses it for exactly
    * this. Without it we have no business touching /vendor libraries.
    */
   void *vndk = dlopen("libvndksupport.so", RTLD_NOW);
   if (!vndk)
      return NULL;

   sphal_load_fn sphal =
      (sphal_load_fn)dlsym(vndk, "android_load_sphal_library");
   if (!sphal)
      return NULL;

   load_imapper_fn load = NULL;
   void *lib = load_vendor_mapper(sphal, &load);
   if (!lib)
      return NULL;

   AIMapper *mapper = NULL;
   if (load(&mapper) != 0 || !mapper)
      return NULL;

   if (mapper->version < AIMAPPER_VERSION_5) {
      mesa_logw("aimapper: version %u is below 5, declining", mapper->version);
      return NULL;
   }

   if (!mapper->v5.getStandardMetadata)
      return NULL;

   struct aimapper_gralloc *gr = CALLOC_STRUCT(aimapper_gralloc);
   if (!gr)
      return NULL;

   gr->mapper_lib = lib;
   gr->mapper = mapper;

   gr->base.ops.get_buffer_basic_info = aimapper_get_buffer_basic_info;
   gr->base.ops.get_buffer_color_info = aimapper_get_buffer_color_info;
   gr->base.ops.destroy = destroy;

   mesa_logi("Using IMapper v5 stable-C API via SP-HAL");

   return &gr->base;
}
