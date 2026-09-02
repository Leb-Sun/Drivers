#!/usr/bin/env python3
"""
Report VK_KHR_shader_integer_dot_product acceleration from dp4acc too, not dp2acc alone.

THE INCONSISTENCY
    ir3 picks the instruction like this (ir3_compiler_nir.c, nir_op_udot_4x8_uadd
    and friends):

        if      (has_dp4acc) emit_alu_dot_4x8_as_dp4acc(...);
        else if (has_dp2acc) emit_alu_dot_4x8_as_dp2acc(...);
        else    ir3_context_error("ALU op should have been lowered");

    dp4acc is the 4-wide instruction and is preferred; dp2acc is the narrower
    fallback for parts that lack it.

    tu_device.cc reports the *Accelerated properties from has_dp2acc only -
    has_dp4acc appears nowhere in that file. On a device with dp4acc but not
    dp2acc the compiler emits hardware DP4ACC while the driver tells applications
    those ops are unaccelerated.

    a840 is exactly that device: a7xx_base sets has_dp4acc = True and a8xx_base
    sets has_dp2acc = False, so all four properties currently report false.

WHAT IT CHANGES
    These properties are informational - the shaderIntegerDotProduct feature makes
    the ops work either way. They tell an app whether the intrinsic is worth using
    over a hand-rolled unpack-and-multiply. Reporting false pushes an app onto the
    slower path for shaders that use SM6.4-style dot4add_u8packed. Narrow, but
    free to fix and strictly more accurate.

    Signed stays false deliberately - upstream's TODO says it can be emulated
    fast enough, and dp4acc's signed handling is a separate question.

Idempotent. Three states per MAINTENANCE.md.
"""
import os
import sys

TU_DEVICE = "src/freedreno/vulkan/tu_device.cc"

# All four sites take the same edit. They are distinguished only by the property
# name on the preceding line, so anchor on the property + assignment together.
EDITS = [
    ("integerDotProduct4x8BitPackedUnsignedAccelerated", ),
    ("integerDotProduct4x8BitPackedMixedSignednessAccelerated", ),
    ("integerDotProductAccumulatingSaturating4x8BitPackedUnsignedAccelerated", ),
    ("integerDotProductAccumulatingSaturating4x8BitPackedMixedSignednessAccelerated", ),
]

OLD_TMPL = """   p->{prop} =
      pdevice->info->props.has_dp2acc;"""
NEW_TMPL = """   p->{prop} =
      pdevice->info->props.has_dp2acc || pdevice->info->props.has_dp4acc;"""

failed = False

if not os.path.exists(TU_DEVICE):
    print(f"  WARNING: {TU_DEVICE} missing", file=sys.stderr)
    sys.exit(1)

with open(TU_DEVICE) as f:
    content = f.read()

applied = skipped = 0
for (prop,) in EDITS:
    old = OLD_TMPL.format(prop=prop)
    new = NEW_TMPL.format(prop=prop)
    if new in content:
        skipped += 1
        continue
    if old not in content:
        print(f"  WARNING: anchor absent for {prop} - upstream refactored?",
              file=sys.stderr)
        failed = True
        continue
    content = content.replace(old, new, 1)
    applied += 1

if applied:
    with open(TU_DEVICE, "w") as f:
        f.write(content)

print(f"  {TU_DEVICE}: {applied} applied, {skipped} already applied")

if failed:
    print("  FATAL: a required anchor was missing", file=sys.stderr)
    sys.exit(1)

print("fix_dot_product_accelerated.py: done")
