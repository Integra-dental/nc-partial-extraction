#!/usr/bin/env python3
# Copyright (c) 2026 Nova Dental LLC (Integra Dental). MIT License, see LICENSE.
"""
nc_extract.py

Map a Fanuc style NC (G-code) program into tool phases and operations, and
write a new partial program to check and run: one phase, one
operation, a line range, or a restart from a chosen line. The new program
keeps the original preamble and tool change block, restores modal state
(units, plane, distance mode, work offset, tool length offset, spindle,
coolant, feed, motion mode), retracts to a safe height, approaches the
restart point, and ends cleanly.

Standard library only. Python 3.8 or newer.

Commands
  check    FILE                 structural lint (things that stop files loading)
  map      FILE                 tool phases, operations, restart candidates
  find     FILE                 locate a block number, text, or XYZ position
  extract  FILE                 one phase, one operation, or a line range
  restart  FILE --line N        everything from line N to the end

Every program this tool writes must be checked in simulation before it runs.
Leave the blank clamped exactly where the job stopped. See README.md.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from array import array

__version__ = "0.1.0"

AXES = ("X", "Y", "Z", "A", "B", "C")
LINEAR = ("X", "Y", "Z")
ROTARY = ("A", "B", "C")

CANNED = {"73", "74", "76", "81", "82", "83", "84", "85", "86", "87", "88", "89"}
MOTION = {"0", "1", "2", "3", "33", "80"} | CANNED
UNITS = {"20", "21"}
PLANES = {"17", "18", "19"}
DIST = {"90", "91"}
FEEDMODE = {"93", "94", "95"}
WCS = {"54", "55", "56", "57", "58", "59"}
CUTCOMP = {"40", "41", "42"}
LENCOMP_ON = {"43", "44", "43.4", "43.5"}
NON_MOTION_AXIS_WORDS = {"4", "10", "65", "66", "52", "92"}  # axis letters are not a move
MACHINE_MOVES = {"53", "28", "30"}  # axis words are machine or reference moves

# per line flags (bitfield in a bytearray)
F_M6 = 1
F_LONE_T = 2
F_G43 = 4
F_FEED = 8      # G1/G2/G3 move with axis words
F_RAPID = 16    # G0 move with axis words in work coordinates
F_END = 32      # M30 or M2
F_AXES = 64     # any work coordinate move
F_MACHINE = 128 # G53/G28/G30 move

K_BLANK, K_COMMENT, K_CODE, K_PERCENT = 0, 1, 2, 3

COMMENT_RE = re.compile(r"\([^)]*\)?|;.*$")
WORD_RE = re.compile(r"([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
NPREFIX_RE = re.compile(r"^(\s*/?\s*[Nn]\d+\s*)")
EOL_RE = re.compile(r"\r\n|\n|\r")


class NCError(Exception):
    """A request the tool refuses because the result would not be safe or valid."""


# ---------------------------------------------------------------- parsing

class Block:
    __slots__ = ("g", "m", "w", "comments", "kind", "macro", "int_axes", "residual")

    def __init__(self):
        self.g = []
        self.m = []
        self.w = {}
        self.comments = []
        self.kind = K_BLANK
        self.macro = False
        self.int_axes = 0
        self.residual = ""


def _norm(text):
    v = float(text)
    return "%g" % v


def parse(raw):
    b = Block()
    comments = b.comments

    def grab(mo):
        t = mo.group(0)
        comments.append(t[1:-1] if t.startswith("(") and t.endswith(")") else t.lstrip("(;"))
        return " "

    code = COMMENT_RE.sub(grab, raw).strip()
    if code.startswith("/"):
        code = code[1:].strip()
    if code.startswith("%"):
        b.kind = K_PERCENT
        return b
    if not code:
        b.kind = K_COMMENT if comments else K_BLANK
        return b
    b.kind = K_CODE
    if "#" in code or "[" in code:
        b.macro = True
    up = code.upper()
    for letter, text in WORD_RE.findall(up):
        if letter == "G":
            b.g.append(_norm(text))
        elif letter == "M":
            b.m.append(_norm(text))
        else:
            b.w[letter] = float(text)
            if letter in AXES and "." not in text:
                b.int_axes += 1
    b.residual = WORD_RE.sub("", up).replace(" ", "").replace("\t", "")
    return b


# ---------------------------------------------------------------- modal state

class State:
    __slots__ = ("units", "plane", "dist", "feedmode", "wcs", "motion", "cutcomp",
                 "lencomp", "h", "t_called", "tool", "spindle", "s", "f",
                 "coolant", "pos", "flags", "ended")

    def __init__(self):
        self.units = None
        self.plane = None
        self.dist = None
        self.feedmode = None
        self.wcs = None
        self.motion = None
        self.cutcomp = None
        self.lencomp = None
        self.h = None
        self.t_called = None
        self.tool = None
        self.spindle = None
        self.s = None
        self.f = None
        self.coolant = frozenset()
        self.pos = dict.fromkeys(AXES)
        self.flags = frozenset()
        self.ended = False

    def copy(self):
        n = State()
        for k in self.__slots__:
            setattr(n, k, getattr(self, k))
        n.pos = dict(self.pos)
        return n


def step(st, b):
    """Apply one parsed block to the modal state. Returns the motion kind of
    the block: None, 'work', or 'machine'."""
    if b.kind != K_CODE:
        return None
    g, m, w = b.g, b.m, b.w
    flags = None
    for c in g:
        if c in UNITS:
            st.units = "G" + c
        elif c in PLANES:
            st.plane = "G" + c
        elif c in DIST:
            st.dist = "G" + c
        elif c in FEEDMODE:
            st.feedmode = "G" + c
        elif c in WCS:
            st.wcs = "G" + c
        elif c == "54.1":
            st.wcs = "G54.1 P%d" % int(w["P"]) if "P" in w else "G54.1"
        elif c in MOTION:
            st.motion = None if c == "80" else "G" + c
        elif c in CUTCOMP:
            st.cutcomp = "G" + c
        elif c in LENCOMP_ON:
            st.lencomp = "G" + c
            if "H" in w:
                st.h = int(w["H"])
        elif c == "49":
            st.lencomp = "G49"
        elif c in ("68", "68.2", "68.3", "51", "51.1", "92"):
            flags = set(st.flags) if flags is None else flags
            flags.add("G" + c)
        elif c in ("69",):
            flags = set(st.flags) if flags is None else flags
            for x in ("G68", "G68.2", "G68.3"):
                flags.discard(x)
        elif c in ("50", "50.1"):
            flags = set(st.flags) if flags is None else flags
            flags.discard("G51")
            flags.discard("G51.1")
        elif c == "52":
            flags = set(st.flags) if flags is None else flags
            if any(w.get(a, 0.0) != 0.0 for a in AXES):
                flags.add("G52")
            else:
                flags.discard("G52")
    if flags is not None:
        st.flags = frozenset(flags)
    if "F" in w and "4" not in g:
        st.f = w["F"]
    if "S" in w:
        st.s = w["S"]
    if "T" in w:
        st.t_called = int(w["T"])
    for c in m:
        if c in ("3", "4", "5"):
            st.spindle = "M" + c
        elif c == "6":
            st.tool = st.t_called
            st.pos = dict.fromkeys(AXES)  # tool change moves the machine
        elif c in ("7", "8"):
            st.coolant = st.coolant | {"M" + c}
        elif c == "9":
            st.coolant = frozenset()
        elif c in ("30", "2"):
            st.ended = True
    axes = [a for a in AXES if a in w]
    if not axes:
        return None
    if any(c in NON_MOTION_AXIS_WORDS for c in g):
        if "52" in g or "92" in g:
            for a in axes:
                st.pos[a] = None
        return None
    if any(c in MACHINE_MOVES for c in g):
        for a in axes:
            st.pos[a] = None
        return "machine"
    if st.motion and st.motion[1:] in CANNED:
        inc = st.dist == "G91"
        for a in ("X", "Y"):
            if a in w:
                st.pos[a] = (None if st.pos[a] is None else st.pos[a] + w[a]) if inc else w[a]
        st.pos["Z"] = None
        return "work"
    inc = st.dist == "G91"
    for a in axes:
        if inc:
            st.pos[a] = None if st.pos[a] is None else st.pos[a] + w[a]
        else:
            st.pos[a] = w[a]
    return "work"


def state_through(lines, stop, start=0, st=None):
    st = st if st is not None else State()
    for i in range(start, stop):
        step(st, parse(lines[i]))
    return st


# ---------------------------------------------------------------- arc length (estimates only)

_PLANE_AXES = {"G17": ("X", "Y", "Z", "I", "J"), "G18": ("Z", "X", "Y", "K", "I"),
               "G19": ("Y", "Z", "X", "J", "K")}


def move_length(prev, cur, b, motion, plane):
    if any(prev[a] is None or cur[a] is None for a in LINEAR):
        return 0.0
    if motion == "G1":
        return math.sqrt(sum((cur[a] - prev[a]) ** 2 for a in LINEAR))
    a1, a2, a3, c1, c2 = _PLANE_AXES.get(plane or "G17")
    p1, p2, q1, q2 = prev[a1], prev[a2], cur[a1], cur[a2]
    helix = cur[a3] - prev[a3]
    chord = math.hypot(q1 - p1, q2 - p2)
    if "R" in b.w:
        r = abs(b.w["R"])
        if r <= 0:
            return chord
        ang = 2 * math.asin(min(1.0, chord / (2 * r)))
        if b.w["R"] < 0:
            ang = 2 * math.pi - ang
    elif c1 in b.w or c2 in b.w:
        cx, cy = p1 + b.w.get(c1, 0.0), p2 + b.w.get(c2, 0.0)
        r = math.hypot(p1 - cx, p2 - cy)
        a0 = math.atan2(p2 - cy, p1 - cx)
        a_end = math.atan2(q2 - cy, q1 - cx)
        if motion == "G2":
            ang = (a0 - a_end) % (2 * math.pi)
        else:
            ang = (a_end - a0) % (2 * math.pi)
        if ang < 1e-9:
            ang = 2 * math.pi
    else:
        return chord
    return math.hypot(r * ang, helix)


# ---------------------------------------------------------------- source and analysis

class Source:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as fh:
            data = fh.read()
        self.size = len(data)
        self.bom = data.startswith(b"\xef\xbb\xbf")
        if self.bom:
            data = data[3:]
        self.non_ascii = sum(1 for ch in data if ch > 127)
        text = data.decode("latin-1")
        crlf = text.count("\r\n")
        lf = text.count("\n") - crlf
        cr = text.count("\r") - crlf
        counts = {"CRLF": crlf, "LF": lf, "CR": cr}
        self.eol_name = max(counts, key=counts.get) if any(counts.values()) else "LF"
        self.eol = {"CRLF": "\r\n", "LF": "\n", "CR": "\r"}[self.eol_name]
        self.mixed_eol = sum(1 for v in counts.values() if v) > 1
        self.lines = EOL_RE.split(text)
        self.final_newline = bool(self.lines) and self.lines[-1] == ""
        if self.final_newline:
            self.lines.pop()
        self.leading_percent = any(l.strip().startswith("%") for l in self.lines[:3])
        self.trailing_percent = any(l.strip().startswith("%") for l in self.lines[-3:])

    @property
    def name(self):
        return os.path.basename(self.path)


class Phase:
    def __init__(self, index, start, m6, tool):
        self.index = index
        self.start = start
        self.m6 = m6
        self.tool = tool
        self.end = None
        self.setup_end = None
        self.ops = []
        self.desc = ""
        self.s = None
        self.h = None
        self.feed_minutes = 0.0
        self.rapids = 0

    def to_dict(self):
        return {
            "phase": self.index, "tool": self.tool,
            "lines": [self.start + 1, self.end + 1],
            "tool_change_block": [self.start + 1, self.setup_end + 1],
            "spindle_s": self.s, "length_offset_h": self.h,
            "description": self.desc,
            "feed_minutes_estimate": round(self.feed_minutes, 2),
            "restart_candidates": self.rapids,
            "operations": [o.to_dict() for o in self.ops],
        }


class Op:
    def __init__(self, index, start, label):
        self.index = index
        self.start = start
        self.end = None
        self.label = label

    def to_dict(self):
        return {"op": self.index, "lines": [self.start + 1, self.end + 1], "label": self.label}


class Analysis:
    pass


def analyze(src, op_pattern=None):
    lines = src.lines
    n = len(lines)
    kinds = bytearray(n)
    flags = bytearray(n)
    feed_t = array("f", bytes(4 * n))
    comments = {}
    s_at, h_at = {}, {}
    an = Analysis()
    an.src = src
    an.n = n
    an.warn = []
    an.counts = dict(g91=0, macro=0, unsupported=0, int_axes=0, cutcomp=0, canned=0,
                     tcp=0, residual=0, lowercase=0, long_lines=0, tabs=0, unbalanced=0,
                     after_end=0, m0=0, blank=0)
    an.rotary_used = set()
    an.retract_text = None
    an.retract_z = None
    an.tools = []
    st = State()
    ended_at = None
    for i, raw in enumerate(lines):
        b = parse(raw)
        kinds[i] = b.kind
        if len(raw) > 256:
            an.counts["long_lines"] += 1
        if "\t" in raw:
            an.counts["tabs"] += 1
        if raw.count("(") != raw.count(")"):
            an.counts["unbalanced"] += 1
        if b.kind == K_BLANK:
            an.counts["blank"] += 1
        if b.kind == K_COMMENT:
            comments[i] = " ".join(c.strip() for c in b.comments if c.strip())
            continue
        if b.kind != K_CODE:
            continue
        if ended_at is not None:
            an.counts["after_end"] += 1
        code_part = COMMENT_RE.sub(" ", raw)
        if any(ch.islower() for ch in code_part):
            an.counts["lowercase"] += 1
        if b.macro:
            an.counts["macro"] += 1
        if b.residual:
            an.counts["residual"] += 1
        if b.int_axes:
            an.counts["int_axes"] += 1
        if "91" in b.g:
            an.counts["g91"] += 1
        if any(c in ("68", "68.2", "68.3", "51", "51.1", "92") for c in b.g) or (
                "52" in b.g and any(b.w.get(a, 0.0) for a in AXES)):
            an.counts["unsupported"] += 1
        if "41" in b.g or "42" in b.g:
            an.counts["cutcomp"] += 1
        if any(c in CANNED for c in b.g):
            an.counts["canned"] += 1
        if "43.4" in b.g or "43.5" in b.g:
            an.counts["tcp"] += 1
        if "0" in b.m or "1" in b.m:
            an.counts["m0"] += 1
        if "S" in b.w:
            s_at[i] = b.w["S"]
        if "H" in b.w and any(c in LENCOMP_ON for c in b.g):
            h_at[i] = int(b.w["H"])
        prev = dict(st.pos)
        kind = step(st, b)
        f = 0
        if "6" in b.m:
            f |= F_M6
            an.tools.append(st.tool)
        if set(b.w) <= {"T", "N"} and "T" in b.w and not b.g and not b.m:
            f |= F_LONE_T
        if any(c in LENCOMP_ON for c in b.g):
            f |= F_G43
        if b.m and ("30" in b.m or "2" in b.m):
            f |= F_END
            if ended_at is None:
                ended_at = i
        if kind == "machine":
            f |= F_MACHINE
            if an.retract_text is None and "Z" in b.w and len([a for a in AXES if a in b.w]) == 1 \
                    and set(b.g) <= {"0", "53", "28", "91", "90"} and not b.m:
                an.retract_text = " ".join(COMMENT_RE.sub(" ", NPREFIX_RE.sub("", raw)).split())
                an.retract_z = b.w["Z"]
        elif kind == "work":
            f |= F_AXES
            for a in ROTARY:
                if a in b.w:
                    an.rotary_used.add(a)
            if st.motion == "G0":
                f |= F_RAPID
            elif st.motion in ("G1", "G2", "G3"):
                f |= F_FEED
                if st.f and st.feedmode in (None, "G94"):
                    feed_t[i] = move_length(prev, st.pos, b, st.motion, st.plane) / st.f
        flags[i] = f
    an.kinds, an.flags, an.feed_t, an.comments = kinds, flags, feed_t, comments
    an.ended_at = ended_at

    # phases
    m6_lines = [i for i in range(n) if flags[i] & F_M6]
    phases = []
    if m6_lines:
        tools = an.tools
        floor = 0
        for k, i6 in enumerate(m6_lines):
            j = i6 - 1
            while j >= floor and (kinds[j] in (K_BLANK, K_COMMENT) or flags[j] & F_LONE_T):
                j -= 1
            start = j + 1
            phases.append(Phase(k + 1, start, i6, tools[k]))
            floor = i6 + 1
    else:
        first = next((i for i in range(n) if flags[i] & (F_AXES | F_FEED | F_RAPID)), None)
        if first is None:
            raise NCError("No tool change (M6) and no work moves found. Is this an NC program?")
        j = first - 1
        while j >= 0 and kinds[j] in (K_BLANK, K_COMMENT):
            j -= 1
        phases.append(Phase(1, j + 1, None, None))
        an.warn.append("No M6 tool change found; the whole program is treated as one phase.")
    for k, ph in enumerate(phases):
        ph.end = phases[k + 1].start - 1 if k + 1 < len(phases) else n - 1
    an.phases = phases
    an.preamble_end = phases[0].start  # exclusive

    pat = re.compile(op_pattern, re.I) if op_pattern else None
    for ph in phases:
        lo = ph.m6 if ph.m6 is not None else ph.start
        se = None
        for i in range(lo, ph.end + 1):
            if flags[i] & F_G43:
                se = i
                break
            if flags[i] & F_FEED:
                break
        if se is None:
            se = lo
            while se + 1 <= ph.end and kinds[se + 1] == K_CODE and not flags[se + 1] & (F_AXES | F_MACHINE):
                se += 1
        ph.setup_end = se
        desc = [comments[i] for i in range(ph.start, se + 1) if i in comments]
        ph.desc = " / ".join(d for d in desc if d)[:120]
        ph.s = next((s_at[i] for i in range(ph.start, ph.end + 1) if i in s_at), None)
        ph.h = next((h_at[i] for i in range(ph.start, ph.end + 1) if i in h_at), None)
        ph.feed_minutes = float(sum(feed_t[ph.start:ph.end + 1]))
        ph.rapids = sum(1 for i in range(se + 1, ph.end + 1) if flags[i] & F_RAPID)
        # operations: runs of comment-only lines in the body
        ops = []
        i = se + 1
        body_moves_before = False
        while i <= ph.end:
            if kinds[i] == K_COMMENT:
                j = i
                texts = []
                while j <= ph.end and kinds[j] in (K_COMMENT, K_BLANK):
                    if kinds[j] == K_COMMENT and comments.get(j):
                        texts.append(comments[j])
                    j += 1
                label = " / ".join(texts)[:120]
                if pat is None or pat.search(label):
                    ops.append(Op(len(ops) + 1, i, label))
                i = j
                continue
            if not ops and flags[i] & (F_FEED | F_RAPID):
                body_moves_before = True
            i += 1
        if body_moves_before:
            ops.insert(0, Op(0, se + 1, "(moves before first comment)"))
            for k, o in enumerate(ops):
                o.index = k + 1
        for k, o in enumerate(ops):
            o.end = ops[k + 1].start - 1 if k + 1 < len(ops) else ph.end
        ph.ops = ops

    c = an.counts
    if c["g91"]:
        an.warn.append("%d block(s) use G91 incremental mode. Restarts inside incremental sections are refused." % c["g91"])
    if c["macro"]:
        an.warn.append("%d block(s) use macro variables or expressions (# or [). They are copied but not understood." % c["macro"])
    if c["unsupported"]:
        an.warn.append("%d block(s) use coordinate rotation, scaling, G52 or G92. Restarts while these are active are refused." % c["unsupported"])
    if c["cutcomp"]:
        an.warn.append("%d block(s) use cutter compensation G41/G42. Restarts while it is active are refused." % c["cutcomp"])
    if c["canned"]:
        an.warn.append("%d block(s) start canned cycles. Restarts inside an active cycle are refused." % c["canned"])
    if c["tcp"]:
        an.warn.append("Program uses G43.4/G43.5 tool center point control. Approach moves are written in work coordinates; check them in simulation.")
    if c["int_axes"]:
        an.warn.append("%d block(s) have axis values without a decimal point. Some controls read those as microns. Inserted moves always use a decimal point." % c["int_axes"])
    if an.rotary_used:
        an.warn.append("Rotary axes used: %s. Restart approaches turn them at the safe height before moving down." % ", ".join(sorted(an.rotary_used)))
    return an


def phase_of(an, line):
    for ph in an.phases:
        if ph.start <= line <= ph.end:
            return ph
    return None


def op_of(ph, line):
    for o in ph.ops:
        if o.start <= line <= o.end:
            return o
    return None


# ---------------------------------------------------------------- formatting

def fmt(v):
    if v is None:
        raise NCError("internal: tried to write an unknown value")
    s = ("%.4f" % v).rstrip("0")
    if s in ("-0.", "-.", ""):
        s = "0."
    return s


def fmt_s(v):
    return str(int(v)) if float(v).is_integer() else fmt(v)


def safe_comment(text, limit=60):
    t = re.sub(r"[^A-Z0-9 .\-_/]", " ", text.upper())
    t = re.sub(r"\s+", " ", t).strip()
    return "(" + t[:limit] + ")"


# ---------------------------------------------------------------- building output

class Plan:
    def __init__(self, mode, phase, body_start, end, entry=None, notes=None):
        self.mode = mode            # 'phase', 'rapid', 'exact'
        self.phase = phase
        self.body_start = body_start
        self.end = end
        self.entry = entry          # line index the restart is anchored to
        self.notes = notes or []


def corrections(cur, tgt, modal_only=False):
    """Lines that bring modal state cur to tgt (groups that matter before cutting)."""
    out = []
    modal = []
    for attr in ("units", "plane", "dist", "feedmode"):
        tv = getattr(tgt, attr)
        if tv and getattr(cur, attr) != tv:
            modal.append(tv)
    if tgt.cutcomp == "G40" and cur.cutcomp not in (None, "G40"):
        modal.append("G40")
    if modal:
        out.append(" ".join(modal))
    if tgt.wcs and cur.wcs != tgt.wcs:
        out.append(tgt.wcs)
    if modal_only:
        return out
    if tgt.lencomp in ("G" + c for c in LENCOMP_ON):
        if cur.lencomp != tgt.lencomp or cur.h != tgt.h:
            out.append("%s H%d" % (tgt.lencomp, tgt.h) if tgt.h is not None else tgt.lencomp)
    elif tgt.lencomp == "G49" and cur.lencomp not in (None, "G49"):
        out.append("G49")
    if tgt.spindle in ("M3", "M4"):
        if cur.spindle != tgt.spindle or (tgt.s is not None and cur.s != tgt.s):
            out.append(("S%s %s" % (fmt_s(tgt.s), tgt.spindle)) if tgt.s is not None else tgt.spindle)
    elif tgt.spindle == "M5" and cur.spindle in ("M3", "M4"):
        out.append("M5")
    if cur.coolant != tgt.coolant:
        if cur.coolant - tgt.coolant:
            out.append("M9")
            have = frozenset()
        else:
            have = cur.coolant
        for c in sorted(tgt.coolant - have):
            out.append(c)
    return out


def compare(st, tgt, check_motion=True):
    bad = []
    for attr in ("units", "plane", "dist", "feedmode", "wcs", "cutcomp", "lencomp", "h", "tool", "spindle"):
        tv = getattr(tgt, attr)
        if tv is not None and getattr(st, attr) != tv:
            bad.append("%s is %s, expected %s" % (attr, getattr(st, attr), tv))
    if tgt.spindle in ("M3", "M4") and tgt.s is not None and st.s != tgt.s:
        bad.append("spindle speed is %s, expected %s" % (st.s, tgt.s))
    if st.coolant != tgt.coolant:
        bad.append("coolant is %s, expected %s" % (sorted(st.coolant), sorted(tgt.coolant)))
    if tgt.f is not None and st.f != tgt.f:
        bad.append("feed is %s, expected %s" % (st.f, tgt.f))
    if check_motion and tgt.motion is not None and st.motion != tgt.motion:
        bad.append("motion mode is %s, expected %s" % (st.motion, tgt.motion))
    for a in AXES:
        tv = tgt.pos[a]
        if tv is not None and (st.pos[a] is None or abs(st.pos[a] - tv) > 1e-6):
            bad.append("%s position is %s, expected %s" % (a, st.pos[a], tv))
    return bad


def retract_lines(an, opts):
    if opts.safe_z is not None:
        return ["G53 G0 Z%s" % fmt(opts.safe_z)]
    if an.retract_text:
        out = [an.retract_text]
        if "G91" in an.retract_text.upper():
            out.append("G90")
        return out
    raise NCError("Could not find a safe Z retract (a G53 or G28 Z move) in this program. "
                  "Pass --safe-z with the machine coordinate Z to retract to.")


def build(an, plan, opts):
    src = an.src
    lines = src.lines
    ph = plan.phase
    warn = list(plan.notes)
    if plan.end < plan.body_start:
        raise NCError("The end line is before the start line.")

    # target state at the body start, taken from the original program
    st_phase = state_through(lines, ph.start)
    stop = plan.entry + 1 if plan.mode == "rapid" else plan.body_start
    tgt = state_through(lines, stop, start=ph.start, st=st_phase.copy())

    if plan.mode in ("rapid", "exact"):
        if tgt.dist == "G91":
            raise NCError("The restart point is in G91 incremental mode, so its position is not known. "
                          "Pick a line in an absolute (G90) section or restart the whole phase.")
        if tgt.flags:
            raise NCError("Coordinate rotation, scaling or shift (%s) is active at the restart point. "
                          "Restart the whole phase instead." % ", ".join(sorted(tgt.flags)))
        if tgt.motion and tgt.motion[1:] in CANNED:
            raise NCError("The restart point is inside an active canned cycle (%s). "
                          "Restart at the line that starts the cycle or restart the phase." % tgt.motion)
        if tgt.cutcomp in ("G41", "G42"):
            raise NCError("Cutter compensation %s is active at the restart point. Restart where it is off (G40)." % tgt.cutcomp)
        if tgt.dist is None:
            warn.append("Distance mode is never set before the restart point; assuming G90 absolute.")
        need = list(LINEAR) + sorted(an.rotary_used)
        missing = [a for a in need if tgt.pos[a] is None]
        if missing:
            raise NCError("The position of axis %s is not known at the restart point "
                          "(the program has not moved it in work coordinates since the tool change). "
                          "Pick a later line or restart the phase." % ", ".join(missing))
        if tgt.lencomp in ("G43.4", "G43.5"):
            warn.append("Tool center point control (%s) is active. Check the approach in simulation." % tgt.lencomp)
        if tgt.motion is None:
            raise NCError("No motion mode is active at the restart point.")
        if tgt.f is None and tgt.motion != "G0":
            raise NCError("No feed rate is set at the restart point.")

    out = []
    comments = not opts.no_comments
    pre = lines[:an.preamble_end]
    ins = 0
    while ins < len(pre) and (not pre[ins].strip() or pre[ins].strip().startswith("%")):
        ins += 1
    if ins < len(pre) and re.match(r"^\s*[O:]\s*\d+", pre[ins], re.I):
        ins += 1
    header = []
    if comments:
        header = [
            safe_comment("PARTIAL PROGRAM BY INTEGRA DENTAL NC EXTRACTION V%s" % __version__),
            safe_comment("SOURCE " + src.name),
            safe_comment("KEEPS SOURCE LINES %d TO %d PHASE %d T%s" % (
                plan.body_start + 1, plan.end + 1, ph.index, ph.tool if ph.tool is not None else "")),
            safe_comment({"phase": "ENTRY PHASE START",
                          "rapid": "ENTRY RESTART AT RAPID LINE %d" % (plan.entry + 1 if plan.entry is not None else 0),
                          "exact": "ENTRY EXACT RESTART AT LINE %d" % (plan.body_start + 1)}[plan.mode]),
            "(LEAVE THE BLANK CLAMPED WHERE IT STOPPED)",
        ]
    out.extend(pre[:ins])
    out.extend(header)
    out.extend(pre[ins:])

    # bring modal groups to what the original had when this phase began
    cur = state_through(out, len(out))
    fix = corrections(cur, st_phase, modal_only=True)
    if fix:
        if comments:
            out.append("(RESTORE MODAL STATE)")
        out.extend(fix)

    # tool change block of the phase, verbatim
    out.extend(lines[ph.start:ph.setup_end + 1])
    cur = state_through(out, len(out))
    fix = corrections(cur, tgt)
    if fix:
        if comments:
            out.append("(RESTORE STATE AT ENTRY)")
        out.extend(fix)

    first_body_override = None
    if plan.mode in ("rapid", "exact"):
        if comments:
            out.append("(SAFE RETRACT AND APPROACH)")
        out.extend(retract_lines(an, opts))
        rot = [a for a in ROTARY if a in an.rotary_used]
        if rot:
            out.append("G0 " + " ".join("%s%s" % (a, fmt(tgt.pos[a])) for a in rot))
        out.append("G0 X%s Y%s" % (fmt(tgt.pos["X"]), fmt(tgt.pos["Y"])))
        z = tgt.pos["Z"]
        units = tgt.units or cur.units
        if plan.mode == "exact":
            clr = opts.clearance if opts.clearance is not None else (0.08 if units == "G20" else 2.0)
            plunge = opts.plunge_feed if opts.plunge_feed is not None else (
                tgt.f * 0.5 if tgt.f else None)
            if plunge is None:
                raise NCError("No feed is known for the plunge. Pass --plunge-feed.")
            out.append("G0 Z%s" % fmt(z + clr))
            out.append("G1 Z%s F%s" % (fmt(z), fmt(plunge)))
            warn.append("Exact restart: the tool rapids to %s above the point, then feeds down at F%s." % (fmt(clr), fmt(plunge)))
        else:
            out.append("G0 Z%s" % fmt(z))
        if tgt.motion in ("G0", "G1"):
            out.append(tgt.motion + ((" F" + fmt(tgt.f)) if tgt.f is not None else ""))
        elif tgt.motion in ("G2", "G3"):
            if tgt.f is not None:
                out.append("F" + fmt(tgt.f))
            first_body_override = tgt.motion
    else:
        if tgt.f is not None and cur.f != tgt.f:
            out.append("F" + fmt(tgt.f))
        if tgt.motion and cur.motion != tgt.motion and tgt.motion in ("G0", "G1"):
            out.append(tgt.motion)
        elif tgt.motion and cur.motion != tgt.motion:
            first_body_override = tgt.motion

    body_out_start = len(out)
    body = list(lines[plan.body_start:plan.end + 1])
    changed = None
    if first_body_override:
        for k, raw in enumerate(body):
            b = parse(raw)
            if b.kind != K_CODE:
                continue
            if any(c in MOTION for c in b.g):
                break
            if any(a in b.w for a in AXES):
                mo = NPREFIX_RE.match(raw)
                body[k] = (mo.group(1) + first_body_override + " " + raw[mo.end():]) if mo else (
                    first_body_override + " " + raw.lstrip())
                changed = plan.body_start + k
                warn.append("Added %s to source line %d so the arc mode carries over." % (first_body_override, changed + 1))
                break
    out.extend(body)

    has_end = any(an.flags[i] & F_END for i in range(plan.body_start, plan.end + 1))
    if not has_end:
        if comments:
            out.append("(END OF PARTIAL PROGRAM)")
        st_end = state_through(out, len(out))
        code_tail = [i for i in range(plan.end, plan.body_start - 1, -1) if an.kinds[i] == K_CODE][:4]
        if not any(an.flags[i] & F_MACHINE for i in code_tail) or st_end.spindle in ("M3", "M4"):
            out.extend(retract_lines(an, opts))
        if st_end.spindle in ("M3", "M4"):
            out.append("M5")
        if st_end.coolant:
            out.append("M9")
        out.append("M30")
        if src.trailing_percent:
            out.append("%")
        if plan.end not in (o.end for o in ph.ops) and plan.end != ph.end and an.flags[plan.end] & F_FEED:
            warn.append("The program ends mid operation (line %d is a cutting move). The retract is straight up in machine Z." % (plan.end + 1))

    # verification: simulate the new program up to the body and compare
    st_out = state_through(out, body_out_start)
    bad = compare(st_out, tgt, check_motion=first_body_override is None)
    if first_body_override:
        st_check = state_through(out, body_out_start + (changed - plan.body_start) + 1) if changed is not None else None
        if st_check is None or st_check.motion != first_body_override:
            bad.append("could not carry the %s arc mode into the body" % first_body_override)
    if not opts.force and bad:
        raise NCError("Verification failed, nothing was written:\n  " + "\n  ".join(bad))
    if bad:
        warn.extend("FORCED past verification: " + x for x in bad)
    for k, raw in enumerate(body):
        if plan.body_start + k != changed and raw != lines[plan.body_start + k]:
            raise NCError("internal: body line changed")
    tools_in_body = [an.tools[k] for k, p in enumerate(an.phases) if p.m6 is not None and plan.body_start <= p.m6 <= plan.end]

    report = {
        "source": src.name,
        "mode": plan.mode,
        "phase": ph.index,
        "tool": ph.tool,
        "kept_source_lines": [plan.body_start + 1, plan.end + 1],
        "entry_line": (plan.entry + 1) if plan.entry is not None else None,
        "output_lines": len(out),
        "inserted_before_body": body_out_start - an.preamble_end - (ph.setup_end + 1 - ph.start),
        "later_tool_changes_kept": tools_in_body,
        "verified": not bad,
        "notes": warn,
    }
    return out, report


# ---------------------------------------------------------------- selection

def snap(an, ph, line, how):
    """Turn a requested start line into a Plan entry."""
    if line < ph.start:
        raise NCError("Line %d is before phase %d." % (line + 1, ph.index))
    if line <= ph.setup_end:
        return "phase", None, ph.setup_end + 1, ["Line %d is in the tool change block of phase %d, so the whole phase is used." % (line + 1, ph.index)]
    if how == "none":
        return "exact", line, line, []
    if how == "phase":
        return "phase", None, ph.setup_end + 1, ["Starting at the beginning of phase %d." % ph.index]
    if how == "op":
        o = op_of(ph, line)
        if o is None:
            return "phase", None, ph.setup_end + 1, ["No operation found around line %d; starting at the phase." % (line + 1)]
        return op_entry(an, ph, o)
    for k in range(line, ph.setup_end, -1):
        if an.flags[k] & F_RAPID:
            note = [] if k == line else ["Moved the start back from line %d to line %d, the last rapid move before it." % (line + 1, k + 1)]
            return "rapid", k, k + 1, note
    return "phase", None, ph.setup_end + 1, ["No rapid move between the tool change and line %d; starting at the phase." % (line + 1)]


def op_entry(an, ph, o):
    for k in range(max(o.start, ph.setup_end + 1), o.end + 1):
        if an.flags[k] & F_RAPID:
            return "rapid", k, k + 1, ["Operation %d starts with a rapid move at line %d." % (o.index, k + 1)]
        if an.flags[k] & F_FEED:
            return "exact", k, k, ["Operation %d starts with a cutting move at line %d; using an exact restart with a slow plunge." % (o.index, k + 1)]
    raise NCError("Operation %d (%s) has no moves." % (o.index, o.label))


def parse_range(text, maximum):
    m = re.match(r"^\s*(\d+)\s*(?:[-:]\s*(\d+)?)?\s*$", text)
    if not m:
        raise NCError("Could not read range %r. Use 5 or 5-9." % text)
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else (a if ":" not in text and "-" not in text else maximum)
    if a < 1 or b > maximum or a > b:
        raise NCError("Range %s is outside 1 to %d." % (text, maximum))
    return a, b


def find_block(an, number):
    rx = re.compile(r"^\s*/?\s*N0*%d(?!\d)" % number, re.I)
    for i, raw in enumerate(an.src.lines):
        if rx.match(raw):
            return i
    raise NCError("Block N%d was not found." % number)


# ---------------------------------------------------------------- commands

def default_out(path, suffix):
    stem, ext = os.path.splitext(path)
    return "%s_%s%s" % (stem, suffix, ext or ".nc")


def write_output(src, out_lines, path):
    text = src.eol.join(out_lines) + src.eol
    data = text.encode("latin-1")
    if path == "-":
        sys.stdout.write(text)
        return
    if os.path.abspath(path) == os.path.abspath(src.path):
        raise NCError("Refusing to overwrite the source program.")
    with open(path, "wb") as fh:
        fh.write(data)


SAFETY = [
    "Check the new program in your CAM simulator or a G-code viewer before loading it. The first approach must reach the restart point from above with clearance.",
    "Leave the blank clamped exactly where the job stopped. Do not remove the part or the tool, or the restart will not line up.",
    "Confirm the work offset, tool number and tool length offset on the control are the same as the original job.",
    "Then run the restart program.",
]


def print_report(report, path, as_json):
    if as_json:
        print(json.dumps(dict(report, output=path, before_running=SAFETY), indent=2))
        return
    print("Wrote %s (%d lines)" % (path, report["output_lines"]))
    print("  phase %s, tool T%s, mode %s, source lines %d to %d" % (
        report["phase"], report["tool"], report["mode"], *report["kept_source_lines"]))
    if report["later_tool_changes_kept"]:
        print("  later tool changes kept: %s" % ", ".join("T%s" % t for t in report["later_tool_changes_kept"]))
    print("  verification: %s" % ("passed" if report["verified"] else "FORCED, see notes"))
    for n in report["notes"]:
        print("  note: " + n)
    print("Before running:")
    for s in SAFETY:
        print("  * " + s)


def cmd_map(args):
    src = Source(args.file)
    an = analyze(src, args.op_pattern)
    if args.json:
        print(json.dumps({
            "file": src.name, "lines": an.n, "line_endings": src.eol_name,
            "preamble": [1, an.preamble_end], "retract": an.retract_text,
            "rotary_axes": sorted(an.rotary_used),
            "phases": [p.to_dict() for p in an.phases], "warnings": an.warn}, indent=2))
        return 0
    print("%s: %s lines, %s line endings" % (src.name, "{:,}".format(an.n), src.eol_name))
    print("Preamble: lines 1 to %d" % an.preamble_end)
    print("Safe retract found: %s" % (an.retract_text or "none (use --safe-z)"))
    total = 0.0
    for p in an.phases:
        total += p.feed_minutes
        print("")
        print("Phase %d  T%s  lines %s to %s  (tool change block to line %s)" % (
            p.index, p.tool, "{:,}".format(p.start + 1), "{:,}".format(p.end + 1), "{:,}".format(p.setup_end + 1)))
        info = []
        if p.s is not None:
            info.append("S%s" % fmt_s(p.s))
        if p.h is not None:
            info.append("H%d" % p.h)
        info.append("about %.1f min of cutting feed" % p.feed_minutes)
        info.append("%d restart points" % p.rapids)
        print("  " + ", ".join(info))
        if p.desc:
            print("  " + p.desc)
        shown = p.ops if args.all_ops else p.ops[:args.max_ops]
        for o in shown:
            print("    op %d  lines %s to %s  %s" % (o.index, "{:,}".format(o.start + 1), "{:,}".format(o.end + 1), o.label))
        if len(p.ops) > len(shown):
            print("    ... %d more operations (use --all-ops)" % (len(p.ops) - len(shown)))
    print("")
    print("Cutting feed time estimate: about %.1f min (feed moves only, ignores rapids and acceleration)" % total)
    for w in an.warn:
        print("warning: " + w)
    return 0


def cmd_check(args):
    src = Source(args.file)
    an = analyze(src)
    c = an.counts
    issues, info = [], []
    if an.ended_at is None:
        issues.append("No program end (M30 or M2) found.")
    if c["after_end"]:
        issues.append("%d code block(s) after the program end." % c["after_end"])
    if src.bom:
        issues.append("File starts with a UTF-8 byte order mark. Some controls refuse it.")
    if src.non_ascii:
        issues.append("%d non ASCII byte(s), often from accented characters in comments or file names." % src.non_ascii)
    if src.mixed_eol:
        issues.append("Mixed line endings (CRLF and LF).")
    if c["unbalanced"]:
        issues.append("%d line(s) with unbalanced parentheses." % c["unbalanced"])
    if c["residual"]:
        issues.append("%d block(s) contain characters that are not G-code words." % c["residual"])
    if c["tabs"]:
        info.append("%d line(s) contain tab characters." % c["tabs"])
    if c["lowercase"]:
        info.append("%d block(s) have lowercase letters outside comments." % c["lowercase"])
    if c["long_lines"]:
        info.append("%d line(s) longer than 256 characters." % c["long_lines"])
    if not src.leading_percent:
        info.append("No % at the start. Some controls need % at the start and end.")
    if c["m0"]:
        info.append("%d program stop(s) M0/M1." % c["m0"])
    if c["blank"]:
        info.append("%d blank line(s)." % c["blank"])
    info.append("Size %s bytes, %s lines, %s line endings, tools %s." % (
        "{:,}".format(src.size), "{:,}".format(an.n), src.eol_name,
        ", ".join("T%s" % t for t in an.tools) or "none"))
    if args.json:
        print(json.dumps({"file": src.name, "issues": issues, "info": info, "warnings": an.warn}, indent=2))
    else:
        print("%s" % src.name)
        for x in issues:
            print("  problem: " + x)
        for x in an.warn:
            print("  warning: " + x)
        for x in info:
            print("  info: " + x)
        if not issues:
            print("  No structural problems found. This does not prove the control will accept it.")
    return 1 if issues else 0


def cmd_find(args):
    src = Source(args.file)
    an = analyze(src)
    hits = []
    if args.block is not None:
        hits = [find_block(an, args.block)]
    elif args.text:
        needle = args.text.upper()
        hits = [i for i, l in enumerate(src.lines) if needle in l.upper()][:args.limit]
    elif args.near:
        target = {}
        for letter, text in WORD_RE.findall(args.near.upper()):
            if letter in AXES:
                target[letter] = float(text)
        if not target:
            raise NCError("Give a position like \"X10.5 Y-2 Z1.2\".")
        st = State()
        scored = []
        for i, raw in enumerate(src.lines):
            if step(st, parse(raw)) == "work" and all(st.pos[a] is not None for a in target):
                d = math.sqrt(sum((st.pos[a] - v) ** 2 for a, v in target.items()))
                if d <= args.tol:
                    scored.append((d, i))
        scored.sort()
        hits = [i for _, i in scored[:args.limit]]
    for i in hits:
        ph = phase_of(an, i)
        o = op_of(ph, i) if ph else None
        where = "preamble" if ph is None else "phase %d T%s" % (ph.index, ph.tool)
        if o:
            where += ", op %d %s" % (o.index, o.label)
        print("line %d  [%s]  %s" % (i + 1, where, src.lines[i].strip()))
    if not hits:
        print("No match.")
        return 1
    return 0


def plan_from_args(an, args, command):
    n = an.n
    phases = an.phases
    if command == "restart":
        if args.block is not None:
            line = find_block(an, args.block)
        elif args.line is not None:
            if not 1 <= args.line <= n:
                raise NCError("Line %d is outside 1 to %d." % (args.line, n))
            line = args.line - 1
        else:
            raise NCError("Give --line or --block.")
        ph = phase_of(an, line)
        if ph is None:
            raise NCError("Line %d is in the preamble, before the first tool change. Run the original program." % (line + 1))
        mode, entry, start, notes = snap(an, ph, line, "none" if args.exact else args.snap)
        if args.until == "phase":
            end = ph.end
        elif args.until == "op":
            o = op_of(ph, line)
            end = o.end if o else ph.end
        else:
            end = n - 1
        return Plan(mode, ph, start, end, entry, notes), "R%d" % (line + 1)

    if args.lines:
        a, b = parse_range(args.lines, n)
        line, end = a - 1, b - 1
        ph = phase_of(an, line)
        if ph is None:
            raise NCError("Line %d is in the preamble, before the first tool change." % a)
        mode, entry, start, notes = snap(an, ph, line, "none" if args.exact else args.snap)
        return Plan(mode, ph, start, end, entry, notes), "L%d-%d" % (a, b)

    if args.op is not None or args.op_match:
        if args.op_match:
            rx = re.compile(args.op_match, re.I)
            found = [(p, o) for p in phases for o in p.ops if rx.search(o.label)
                     and (args.phase is None or str(p.index) == str(args.phase))]
            if not found:
                raise NCError("No operation label matches %r. Run map to see the labels." % args.op_match)
            if len(found) > 1 and not args.first:
                raise NCError("%d operations match %r: %s. Narrow it with --phase or use --first." % (
                    len(found), args.op_match, "; ".join("phase %d op %d %s" % (p.index, o.index, o.label) for p, o in found[:8])))
            ph, o = found[0]
        else:
            if args.phase is None:
                raise NCError("--op needs --phase (operation numbers restart in each phase).")
            ph = phases[int(args.phase) - 1] if 1 <= int(args.phase) <= len(phases) else None
            if ph is None:
                raise NCError("There is no phase %s." % args.phase)
            if not 1 <= args.op <= len(ph.ops):
                raise NCError("Phase %d has %d operations." % (ph.index, len(ph.ops)))
            o = ph.ops[args.op - 1]
        mode, entry, start, notes = op_entry(an, ph, o)
        end = n - 1 if args.through_end else o.end
        return Plan(mode, ph, start, end, entry, notes), "P%dOP%d" % (ph.index, o.index)

    if args.tool is not None:
        tool = int(str(args.tool).upper().lstrip("T"))
        match = [p for p in phases if p.tool == tool]
        if not match:
            raise NCError("No phase uses T%d. Tools in this program: %s." % (tool, ", ".join("T%s" % p.tool for p in phases)))
        if len(match) > 1:
            raise NCError("T%d is used in phases %s. Pick one with --phase." % (tool, ", ".join(str(p.index) for p in match)))
        a = b = match[0].index
    elif args.phase is not None:
        a, b = parse_range(str(args.phase), len(phases))
    else:
        raise NCError("Choose what to extract: --phase, --tool, --op, --op-match or --lines.")
    ph = phases[a - 1]
    end = n - 1 if args.through_end else phases[b - 1].end
    return Plan("phase", ph, ph.setup_end + 1, end, None, []), ("P%d" % a if a == b else "P%d-%d" % (a, b))


def cmd_extract(args, command):
    src = Source(args.file)
    an = analyze(src, getattr(args, "op_pattern", None))
    plan, suffix = plan_from_args(an, args, command)
    out, report = build(an, plan, args)
    path = args.output or default_out(args.file, suffix)
    write_output(src, out, path)
    if path != "-":
        print_report(report, path, args.json)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(
        prog="nc_extract",
        description="Map Fanuc style NC programs and write safe partial programs (one phase, one operation, "
                    "a line range, or a restart). Check the result in simulation and keep the blank clamped where it stopped.")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    sub = p.add_subparsers(dest="command", required=True)

    def common_out(sp):
        sp.add_argument("-o", "--output", help="output file (default: next to the source with a suffix; - for stdout)")
        sp.add_argument("--safe-z", type=float, help="machine Z for the safe retract, written as G53 G0 Z (default: copy the program's own retract)")
        sp.add_argument("--clearance", type=float, help="exact restart: height above the point to rapid to (default 2 mm or 0.08 in)")
        sp.add_argument("--plunge-feed", type=float, help="exact restart: feed for the last move down (default half the active feed)")
        sp.add_argument("--no-comments", action="store_true", help="do not add explanatory comment lines")
        sp.add_argument("--force", action="store_true", help="write even if verification finds a difference (not recommended)")
        sp.add_argument("--json", action="store_true", help="print the report as JSON")
        sp.add_argument("--snap", choices=["rapid", "op", "phase"], default="rapid",
                        help="where to move a mid program start: last rapid move (default), operation start, or phase start")
        sp.add_argument("--exact", action="store_true", help="restart exactly at the line with a slow plunge (no snapping)")

    sp = sub.add_parser("map", help="show tool phases, operations and restart points")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--op-pattern", help="only treat comments matching this regex as operation starts")
    sp.add_argument("--max-ops", type=int, default=25)
    sp.add_argument("--all-ops", action="store_true")

    sp = sub.add_parser("check", help="look for structural problems that stop a file loading")
    sp.add_argument("file")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("find", help="find a block number, text, or the lines nearest a position")
    sp.add_argument("file")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--block", type=int, help="block number, for N1234 give 1234")
    g.add_argument("--text", help="text to search for")
    g.add_argument("--near", help='position like "X10.5 Y-2 Z1.2" (work coordinates)')
    sp.add_argument("--tol", type=float, default=0.05, help="distance tolerance for --near (default 0.05)")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("extract", help="write one phase, one operation, or a line range as a new program")
    sp.add_argument("file")
    sp.add_argument("--phase", help="phase number or range, like 2 or 2-3")
    sp.add_argument("--tool", help="phase by tool number, like 3 or T3")
    sp.add_argument("--op", type=int, help="operation number within --phase")
    sp.add_argument("--op-match", help="operation whose label matches this regex, like support")
    sp.add_argument("--op-pattern", help="only treat comments matching this regex as operation starts")
    sp.add_argument("--first", action="store_true", help="with --op-match, take the first match")
    sp.add_argument("--lines", help="source line range, like 1200-5400")
    sp.add_argument("--through-end", action="store_true", help="keep everything after the selection to the program end")
    common_out(sp)

    sp = sub.add_parser("restart", help="write a program that restarts at a line and runs to the end")
    sp.add_argument("file")
    sp.add_argument("--line", type=int, help="source line number (1 based)")
    sp.add_argument("--block", type=int, help="block number, for N1234 give 1234")
    sp.add_argument("--until", choices=["end", "phase", "op"], default="end",
                    help="where to stop: program end (default), end of this phase, or end of this operation")
    common_out(sp)

    args = p.parse_args(argv)
    try:
        if args.command == "map":
            return cmd_map(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "find":
            return cmd_find(args)
        return cmd_extract(args, args.command)
    except NCError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
