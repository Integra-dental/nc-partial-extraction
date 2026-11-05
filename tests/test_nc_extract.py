"""Tests for nc_extract.py. Run from the repository root:

    python3 -m unittest discover -s tests -v

All programs are synthetic (see make_samples.py).
"""

import io
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "skills", "integra-dental-nc-partial-extraction", "scripts"))

import nc_extract as nx  # noqa: E402
from make_samples import dental_program, single_tool_program, write  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = nx.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def read(path):
    with open(path, newline="") as fh:
        return fh.read()


def tools_changed(lines):
    return [int(m.group(1)) for l in lines for m in [re.search(r"T(\d+)\s*M0?6\b", l)] if m]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def sample(self, lines=None, name="job.nc", eol="\n"):
        path = os.path.join(self.dir, name)
        write(path, lines if lines is not None else dental_program(scale=3), eol)
        return path

    def out(self, name="out.nc"):
        return os.path.join(self.dir, name)

    def assert_state_matches(self, src_lines, out_lines, src_index, out_index, check_pos=True):
        """State of the original before src_index equals state of the output before out_index."""
        a = nx.state_through(src_lines, src_index)
        b = nx.state_through(out_lines, out_index)
        for attr in ("units", "plane", "dist", "wcs", "tool", "lencomp", "h", "spindle", "s", "coolant", "f"):
            self.assertEqual(getattr(a, attr), getattr(b, attr), attr)
        if check_pos:
            for ax in nx.AXES:
                if a.pos[ax] is not None:
                    self.assertAlmostEqual(a.pos[ax], b.pos[ax], places=6, msg=ax)


class MapTests(Base):
    def test_phases_and_ops(self):
        src = nx.Source(self.sample())
        an = nx.analyze(src)
        self.assertEqual([p.tool for p in an.phases], [1, 2, 3])
        self.assertEqual(an.retract_text, "G53 G0 Z0.")
        p1, p2, p3 = an.phases
        self.assertEqual(src.lines[p1.start].strip(), "(T1 FLAT END MILL D2.5 ROUGHING)")
        self.assertIn("G43 H1", src.lines[p1.setup_end])
        self.assertEqual(p2.start, p1.end + 1)
        self.assertEqual([o.label for o in p3.ops], ["DETAIL FISSURES", "SUPPORT CUT OFF"])
        self.assertEqual(p2.s, 24000)
        self.assertGreater(p1.feed_minutes, 0)
        self.assertEqual(sorted(an.rotary_used), ["A", "B"])

    def test_map_cli_and_json(self):
        path = self.sample()
        code, out, _ = run("map", path)
        self.assertEqual(code, 0)
        self.assertIn("Phase 3  T3", out)
        code, out, _ = run("map", path, "--json")
        import json
        data = json.loads(out)
        self.assertEqual(len(data["phases"]), 3)

    def test_preselect_t_does_not_split_phase(self):
        an = nx.analyze(nx.Source(self.sample()))
        # "T2" after "T1 M6" is a preselect, phase 1 still ends at the second M6
        self.assertEqual(len(an.phases), 3)
        self.assertEqual(an.phases[0].tool, 1)


class ExtractPhaseTests(Base):
    def test_phase_two_only(self):
        path = self.sample()
        o = self.out()
        code, _, err = run("extract", path, "--phase", "2", "-o", o)
        self.assertEqual(code, 0, err)
        src = nx.Source(path)
        lines = read(o).splitlines()
        self.assertEqual(tools_changed(lines), [2])
        self.assertEqual(lines[0], "%")
        self.assertEqual(lines[1], "O1001")
        self.assertEqual(lines[-2:], ["M30", "%"])
        an = nx.analyze(src)
        p2 = an.phases[1]
        body = src.lines[p2.setup_end + 1:p2.end + 1]
        joined = "\n".join(lines)
        self.assertIn("\n".join(body), joined)
        self.assertIn("G54", lines)  # set in phase 1 in the source, restored here
        k = lines.index(body[0])
        self.assert_state_matches(src.lines, lines, p2.setup_end + 1, k, check_pos=False)

    def test_tool_selector_and_range(self):
        path = self.sample()
        code, _, err = run("extract", path, "--tool", "T3", "-o", self.out("t3.nc"))
        self.assertEqual(code, 0, err)
        self.assertEqual(tools_changed(read(self.out("t3.nc")).splitlines()), [3])
        code, _, err = run("extract", path, "--phase", "2-3", "-o", self.out("p23.nc"))
        self.assertEqual(code, 0, err)
        self.assertEqual(tools_changed(read(self.out("p23.nc")).splitlines()), [2, 3])

    def test_coolant_carried_when_phase_relies_on_it(self):
        lines = dental_program(scale=1)
        # remove the M8 from phase 2 so it relies on phase 1's coolant
        i = [k for k, l in enumerate(lines) if l == "T2 M6"][0]
        j = lines.index("M8", i)
        del lines[j]
        # and keep coolant on between phases
        k = lines.index("M9", 0)
        del lines[k]
        path = self.sample(lines)
        o = self.out()
        code, _, err = run("extract", path, "--phase", "2", "-o", o)
        self.assertEqual(code, 0, err)
        self.assertIn("M8", read(o).splitlines())

    def test_never_overwrites_source(self):
        path = self.sample()
        code, _, err = run("extract", path, "--phase", "1", "-o", path)
        self.assertEqual(code, 2)
        self.assertIn("overwrite", err)


class RestartTests(Base):
    def test_restart_snaps_to_rapid(self):
        path = self.sample()
        src = nx.Source(path)
        an = nx.analyze(src)
        p2 = an.phases[1]
        # pick a cutting line in the middle of phase 2
        line = next(i for i in range(p2.setup_end + 40, p2.end) if an.flags[i] & nx.F_FEED)
        o = self.out()
        code, out, err = run("restart", path, "--line", str(line + 1), "-o", o)
        self.assertEqual(code, 0, err)
        self.assertIn("Moved the start back", out)
        lines = read(o).splitlines()
        self.assertEqual(tools_changed(lines), [2, 3])
        rapid = max(i for i in range(p2.setup_end + 1, line + 1) if an.flags[i] & nx.F_RAPID)
        tail = src.lines[rapid + 1:]
        self.assertEqual(lines[-len(tail):], tail)
        k = len(lines) - len(tail)
        self.assert_state_matches(src.lines, lines, rapid + 1, k)
        # approach order: retract, rotary, XY, Z
        approach = lines[lines.index("(SAFE RETRACT AND APPROACH)") + 1:k]
        self.assertEqual(approach[0], "G53 G0 Z0.")
        self.assertTrue(approach[1].startswith("G0 A"))
        self.assertTrue(approach[2].startswith("G0 X"))
        self.assertTrue(approach[3].startswith("G0 Z"))
        # T1 never runs
        self.assertNotIn("T1 M6", lines)

    def test_exact_restart_plunges_slowly(self):
        path = self.sample()
        src = nx.Source(path)
        an = nx.analyze(src)
        p1 = an.phases[0]
        line = next(i for i in range(p1.setup_end + 30, p1.end) if an.flags[i] & nx.F_FEED)
        o = self.out()
        code, _, err = run("restart", path, "--line", str(line + 1), "--exact", "--until", "phase", "-o", o)
        self.assertEqual(code, 0, err)
        lines = read(o).splitlines()
        before = nx.state_through(src.lines, line)
        z = before.pos["Z"]
        self.assertIn("G0 Z%s" % nx.fmt(z + 2.0), lines)
        self.assertIn("G1 Z%s F%s" % (nx.fmt(z), nx.fmt(before.f * 0.5)), lines)
        k = lines.index(src.lines[line], lines.index("(SAFE RETRACT AND APPROACH)"))
        self.assert_state_matches(src.lines, lines, line, k)
        self.assertEqual(tools_changed(lines), [1])
        self.assertEqual(lines[-1], "%")
        self.assertIn("M30", lines)

    def test_exact_restart_on_arc_line(self):
        path = self.sample()
        src = nx.Source(path)
        idx = next(i for i, l in enumerate(src.lines) if l.startswith("X") and " I" in l)
        o = self.out()
        code, out, err = run("restart", path, "--line", str(idx + 1), "--exact", "-o", o)
        self.assertEqual(code, 0, err)
        lines = read(o).splitlines()
        self.assertIn("G3 " + src.lines[idx], lines)
        self.assertIn("Added G3", out)

    def test_op_extract_support_cut_off(self):
        path = self.sample()
        o = self.out()
        code, out, err = run("extract", path, "--op-match", "support", "-o", o)
        self.assertEqual(code, 0, err)
        lines = read(o).splitlines()
        self.assertEqual(tools_changed(lines), [3])
        self.assertNotIn("(DETAIL FISSURES)", lines)
        self.assertEqual(sum(1 for l in lines if l.startswith("G1 X-16.")), 3)
        self.assertEqual(lines[-2:], ["M30", "%"])

    def test_op_by_number(self):
        path = self.sample()
        code, _, err = run("extract", path, "--phase", "1", "--op", "2", "-o", self.out())
        self.assertEqual(code, 0, err)
        lines = read(self.out()).splitlines()
        self.assertIn("G0 A0. B180.", lines)  # rotary positioning of the back side
        self.assertEqual(tools_changed(lines), [1])

    def test_line_range_adds_clean_end(self):
        path = self.sample()
        src = nx.Source(path)
        an = nx.analyze(src)
        p1 = an.phases[0]
        a = p1.setup_end + 20
        b = a + 30
        o = self.out()
        code, out, err = run("extract", path, "--lines", "%d-%d" % (a, b), "-o", o)
        self.assertEqual(code, 0, err)
        lines = read(o).splitlines()
        end = lines[lines.index("(END OF PARTIAL PROGRAM)") + 1:]
        self.assertEqual(end, ["G53 G0 Z0.", "M5", "M9", "M30", "%"])

    def test_block_number(self):
        lines = dental_program(scale=2)
        numbered = []
        n = 0
        for l in lines:
            if l and not l.startswith("%") and not l.startswith("O") and not l.startswith("("):
                n += 10
                numbered.append("N%d %s" % (n, l))
            else:
                numbered.append(l)
        path = self.sample(numbered)
        src = nx.Source(path)
        target = next(l for l in src.lines if l.startswith("N") and " G1 " in l and src.lines.index(l) > 250)
        num = int(target.split()[0][1:])
        code, _, err = run("restart", path, "--block", str(num), "-o", self.out())
        self.assertEqual(code, 0, err)
        code, out, _ = run("find", path, "--block", str(num))
        self.assertIn(target.strip(), out)

    def test_restart_in_tool_change_block_uses_phase(self):
        path = self.sample()
        an = nx.analyze(nx.Source(path))
        p2 = an.phases[1]
        code, out, err = run("restart", path, "--line", str(p2.m6 + 1), "-o", self.out())
        self.assertEqual(code, 0, err)
        self.assertIn("tool change block", out)


class RefusalTests(Base):
    def test_refuses_incremental(self):
        path = self.sample(dental_program(scale=1, extras=True))
        src = nx.Source(path)
        idx = src.lines.index("X1.", src.lines.index("G91"))
        code, _, err = run("restart", path, "--line", str(idx + 1), "--exact", "-o", self.out())
        self.assertEqual(code, 2)
        self.assertIn("G91", err)
        self.assertFalse(os.path.exists(self.out()))

    def test_refuses_inside_canned_cycle(self):
        path = self.sample(dental_program(scale=1, extras=True))
        src = nx.Source(path)
        idx = src.lines.index("X15. Y0.")
        code, _, err = run("restart", path, "--line", str(idx + 1), "--exact", "-o", self.out())
        self.assertEqual(code, 2)
        self.assertIn("canned cycle", err)

    def test_snap_skips_back_over_incremental_section(self):
        path = self.sample(dental_program(scale=1, extras=True))
        src = nx.Source(path)
        idx = src.lines.index("X1.", src.lines.index("G91"))
        code, out, err = run("restart", path, "--line", str(idx + 1), "-o", self.out())
        # snapping goes back to the rapid before G91, which is absolute
        self.assertEqual(code, 0, err)
        self.assertIn("G91", read(self.out()))

    def test_needs_safe_z_when_no_retract(self):
        lines = [l for l in dental_program(scale=1) if not l.startswith("G53")]
        path = self.sample(lines)
        an = nx.analyze(nx.Source(path))
        line = next(i for i in range(an.phases[1].setup_end + 5, an.phases[1].end) if an.flags[i] & nx.F_FEED)
        code, _, err = run("restart", path, "--line", str(line + 1), "-o", self.out())
        self.assertEqual(code, 2)
        self.assertIn("--safe-z", err)
        code, _, err = run("restart", path, "--line", str(line + 1), "--safe-z", "0", "-o", self.out())
        self.assertEqual(code, 0, err)
        self.assertIn("G53 G0 Z0.", read(self.out()).splitlines())

    def test_preamble_line_refused(self):
        path = self.sample()
        code, _, err = run("restart", path, "--line", "3", "-o", self.out())
        self.assertEqual(code, 2)
        self.assertIn("preamble", err)


class FormatTests(Base):
    def test_crlf_preserved(self):
        path = self.sample(eol="\r\n")
        o = self.out()
        code, _, err = run("extract", path, "--phase", "3", "-o", o)
        self.assertEqual(code, 0, err)
        data = read(o)
        self.assertIn("\r\n", data)
        self.assertNotIn("\n", data.replace("\r\n", ""))

    def test_single_tool_program(self):
        path = self.sample(single_tool_program(), name="single.nc")
        an = nx.analyze(nx.Source(path))
        self.assertEqual(len(an.phases), 1)
        src = nx.Source(path)
        idx = src.lines.index("G1 X8. F900.")
        code, _, err = run("restart", path, "--line", str(idx + 1), "-o", self.out())
        self.assertEqual(code, 0, err)
        lines = read(self.out()).splitlines()
        self.assertIn("G0 X5. Y5.", lines)  # approach to the rapid it snapped to
        self.assertEqual(lines[-2:], ["M30", "%"])

    def test_number_format_keeps_decimal_point(self):
        self.assertEqual(nx.fmt(10), "10.")
        self.assertEqual(nx.fmt(-0.0), "0.")
        self.assertEqual(nx.fmt(1.23456), "1.2346")
        self.assertEqual(nx.fmt(-2.5), "-2.5")

    def test_check_flags_missing_end(self):
        lines = [l for l in dental_program(scale=1) if l != "M30"]
        path = self.sample(lines)
        code, out, _ = run("check", path)
        self.assertEqual(code, 1)
        self.assertIn("No program end", out)

    def test_find_near_position(self):
        path = self.sample()
        code, out, _ = run("find", path, "--near", "X-16 Y0 Z-1", "--tol", "0.01")
        self.assertEqual(code, 0)
        self.assertIn("SUPPORT CUT OFF", out)

    def test_restart_guidance_keeps_blank_clamped(self):
        path = self.sample()
        code, out, err = run("extract", path, "--phase", "2", "-o", self.out())
        self.assertEqual(code, 0, err)
        text = read(self.out())
        self.assertIn("(LEAVE THE BLANK CLAMPED WHERE IT STOPPED)", text)
        self.assertIn("Leave the blank clamped exactly where the job stopped", out)
        for said in (text, out, "\n".join(nx.SAFETY)):
            low = said.lower()
            for banned in ("air cut", "dry run", "feed hold", "feed override", "prove out"):
                self.assertNotIn(banned, low)

    def test_no_comments_option(self):
        path = self.sample()
        code, _, err = run("extract", path, "--phase", "2", "--no-comments", "-o", self.out())
        self.assertEqual(code, 0, err)
        self.assertNotIn("(PARTIAL PROGRAM", read(self.out()))


class ScaleTest(Base):
    def test_large_program(self):
        lines = dental_program(scale=400)
        path = self.sample(lines, name="big.nc")
        t = time.time()
        an = nx.analyze(nx.Source(path))
        mid = next(i for i in range(an.phases[1].setup_end + 1000, an.n) if an.flags[i] & nx.F_FEED)
        code, _, err = run("restart", path, "--line", str(mid + 1), "-o", self.out())
        self.assertEqual(code, 0, err)
        self.assertLess(time.time() - t, 60)


if __name__ == "__main__":
    unittest.main()
