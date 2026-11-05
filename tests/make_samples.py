#!/usr/bin/env python3
"""Generate synthetic NC programs for the tests and examples.

These programs are made up. They imitate the shape of a 5 axis dental
milling job (three tools, rotary positioning, roughing, finishing, a
support cut off pass) but contain no real case, patient or customer data.
"""

import math
import os
import sys


def _zigzag(out, x0, x1, y0, y1, z, step, feed):
    y = y0
    forward = True
    out.append("G1 Z%.3f F%.1f" % (z, feed / 2))
    first = True
    while y <= y1 + 1e-9:
        xa, xb = (x0, x1) if forward else (x1, x0)
        if first:
            out.append("G1 X%.3f Y%.3f F%.1f" % (xb, y, feed))
            first = False
        else:
            out.append("X%.3f Y%.3f" % (xa, y))
            out.append("X%.3f" % xb)
        forward = not forward
        y += step


def _arcs(out, cx, cy, r, z, n):
    out.append("G1 X%.3f Y%.3f Z%.3f" % (cx + r, cy, z))
    for _ in range(n):
        out.append("G3 X%.3f Y%.3f I%.3f J0." % (cx - r, cy, -r))
        out.append("X%.3f Y%.3f I%.3f J0." % (cx + r, cy, r))


def dental_program(scale=1, extras=False):
    """Return a list of lines. scale multiplies the cutting passes."""
    o = []
    o += ["%", "O1001", "(SYNTHETIC SAMPLE PROGRAM, NOT A REAL CASE)",
          "(MATERIAL ZIRCONIA DISC 98 X 20)",
          "G90 G17 G21 G40 G80 G94", "G53 G0 Z0."]
    # phase 1: roughing
    o += ["(T1 FLAT END MILL D2.5 ROUGHING)", "T1 M6", "T2", "S18000 M3", "M8", "G54",
          "G0 A0. B0.", "G0 X0. Y0.", "G43 H1 Z15."]
    for side, b in (("FRONT", 0.0), ("BACK", 180.0)):
        o.append("(ROUGHING %s)" % side)
        o.append("G0 B%.1f" % b)
        for k in range(scale):
            z = -0.5 - 0.5 * k
            o.append("G0 X-12. Y-10. Z3.")
            _zigzag(o, -12.0, 12.0, -10.0, 10.0, z, 1.5, 1800.0)
            o.append("G0 Z3.")
        if extras and side == "FRONT":
            o.append("(DRILL REFERENCE HOLES)")
            o.append("G0 X-15. Y0. Z5.")
            o.append("G81 X-15. Y0. Z-2. R1. F200.")
            o.append("X15. Y0.")
            o.append("G80")
            o.append("G0 Z5.")
    o += ["G53 G0 Z0.", "M5", "M9"]
    # phase 2: finishing with arcs and a speed change
    o += ["(T2 BALL END MILL D1.0 FINISHING)", "T2 M6", "T3", "S24000 M3", "M8",
          "G0 A0. B0.", "G0 X0. Y0.", "G43 H2 Z15."]
    o.append("(FINISH OCCLUSAL)")
    for k in range(scale):
        o.append("G0 X5. Y0. Z2.")
        _arcs(o, 0.0, 0.0, 5.0 - 0.2 * (k % 10), -0.8 - 0.05 * k, 4)
        o.append("G0 Z2.")
    o.append("(FINISH MARGIN)")
    o.append("S22000")
    o.append("G0 A15. B90.")
    for k in range(scale):
        o.append("G0 X-6. Y-6. Z2.")
        _zigzag(o, -6.0, 6.0, -6.0, 6.0, -1.5 - 0.05 * k, 0.4, 1200.0)
        o.append("G0 Z2.")
    if extras:
        o.append("(INCREMENTAL ENGRAVE)")
        o.append("G0 X20. Y20. Z1.")
        o.append("G91")
        o.append("G1 Z-1.2 F300.")
        o.append("X1.")
        o.append("Y1.")
        o.append("G90")
        o.append("G0 Z3.")
    o += ["G53 G0 Z0.", "M5", "M9"]
    # phase 3: small ball, detail and support cut off
    o += ["(T3 BALL END MILL D0.6 DETAIL AND SUPPORT CUT OFF)", "T3 M6", "S28000 M3", "M8",
          "G0 A0. B0.", "G0 X0. Y0.", "G43 H3 Z15."]
    o.append("(DETAIL FISSURES)")
    for k in range(scale):
        o.append("G0 X-3. Y-2. Z1.")
        _zigzag(o, -3.0, 3.0, -2.0, 2.0, -0.9 - 0.02 * k, 0.15, 900.0)
        o.append("G0 Z1.")
    o.append("(SUPPORT CUT OFF)")
    o.append("G0 A0. B-90.")
    for k in range(3):
        o.append("G0 X-14. Y%.2f Z2." % (-1.0 + k))
        o.append("G1 Z-%.2f F400." % (0.5 + 0.5 * k))
        o.append("G1 X-16. F600.")
        o.append("G0 Z2.")
    o += ["G53 G0 Z0.", "M5", "M9", "G0 A0. B0.", "M30", "%"]
    return o


def single_tool_program():
    return ["%", "O2002", "(SINGLE TOOL, NO M6)", "G90 G17 G21 G94", "G53 G0 Z0.",
            "S20000 M3", "M8", "G54", "G43 H1 Z10.", "G0 X0. Y0. Z2.", "G1 Z-0.5 F300.",
            "G1 X10. F900.", "Y10.", "X0.", "Y0.", "G0 Z2.", "G0 X5. Y5.", "G1 Z-1. F300.",
            "G1 X8. F900.", "G0 Z2.", "G53 G0 Z0.", "M5", "M9", "M30", "%"]


def write(path, lines, eol="\n"):
    with open(path, "w", newline="") as fh:
        fh.write(eol.join(lines) + eol)


if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "examples")
    os.makedirs(outdir, exist_ok=True)
    write(os.path.join(outdir, "sample_crown.nc"), dental_program(scale=3))
    write(os.path.join(outdir, "sample_single_tool.nc"), single_tool_program())
    print("wrote samples to", os.path.abspath(outdir))
