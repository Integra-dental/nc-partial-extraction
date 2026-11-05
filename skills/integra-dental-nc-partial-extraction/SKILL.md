---
name: integra-dental-nc-partial-extraction
description: Map, split and restart Fanuc style CNC milling programs (G-code .nc files, such as dental lab programs posted from MillBox or similar CAM for 5 axis mills). Use when someone wants to restart a long job after an alarm or stop without rerunning earlier tools, run only one tool phase or one operation (for example the final support cut off pass), cut a line range into its own program, find where a block number or position sits in a program, or work out why a program will not load. Produces a new runnable program that keeps the preamble and tool change, restores modal state, retracts and approaches safely, and ends cleanly.
---

# Integra Dental NC Partial Extraction

This skill turns one big NC program into a smaller program you can check and run. It ships with a standard library Python script, `scripts/nc_extract.py`, that does the parsing, the state tracking and a verification pass. Use the script instead of editing G-code by hand: hand edits to million line files are where crashes come from.

The person you are helping is usually a lab technician or CAM operator standing at a machine that stopped. Be quick, be concrete, and never tell them a file is safe. Tell them what the file does and how to restart the job.

## What the script understands

* Fanuc style blocks: G, M, T, S, F, H, X, Y, Z, A, B, C, I, J, K, R, N words, comments in parentheses or after `;`, `%` and `O` program numbers.
* Tool phases start at each `M6` tool change. Comment lines and a lone `T` line right before the `M6` belong to the new phase. A `T` on its own after an `M6` is a preselect and does not split a phase.
* The tool change block of a phase runs from the phase start through the first `G43` (or `G43.4`, `G43.5`) line. If the phase has no `G43`, it runs through the lines after `M6` that do not move.
* Operations are runs of comment only lines inside a phase (CAM posts usually write the operation name as a comment). `--op-pattern` limits which comments count.
* Modal state tracked per line: units G20/G21, plane G17/G18/G19, G90/G91, feed mode G93/G94/G95, work offset G54 to G59 and G54.1 Pn, motion mode including canned cycles, G40/G41/G42, G43/G49 with H, active tool, spindle M3/M4/M5 and S, coolant M7/M8/M9, feed F, and the position of every axis in work coordinates. `M6` and G53/G28/G30 moves make positions unknown until the program moves again.
* Restarts are refused, with a clear reason, when the restart point is in G91 incremental mode, inside an active canned cycle, with cutter compensation on, or with G68 rotation, G51 scaling, G52 or G92 active. Restart the whole phase in those cases.

## Workflow

1. **Get the file.** Ask for the `.nc` file if you do not have it. Work on a copy. Never overwrite the original (the script refuses to).

2. **Check and map it.**
   ```
   python3 scripts/nc_extract.py check JOB.nc
   python3 scripts/nc_extract.py map JOB.nc
   ```
   Show the person the phase table: tool, line range, operations, estimated cutting time. Use `map --json` when you need to reason over it. If `check` reports problems (no M30, byte order mark, non ASCII characters, unbalanced parentheses, code after M30), that is often the answer to "why won't this file open". `check` only finds structural problems; it cannot promise a control will accept the file.

3. **Pin down what they want.** Ask only what you cannot see in the map.
   * Restart after an alarm: which line or block number was running, or which operation. If they only know the machine position, use `find --near "X.. Y.. Z.."` (work coordinates). If they know the N number, use `find --block 1234`.
   * One tool: which phase or tool number.
   * One operation: the label or number from the map.
   * A line range: start and end lines.

4. **Write the partial program.** Pick one:
   ```
   # one phase (one tool), or a range of phases
   python3 scripts/nc_extract.py extract JOB.nc --phase 3
   python3 scripts/nc_extract.py extract JOB.nc --tool T2
   python3 scripts/nc_extract.py extract JOB.nc --phase 2-3

   # one operation, by label or by number
   python3 scripts/nc_extract.py extract JOB.nc --op-match "support"
   python3 scripts/nc_extract.py extract JOB.nc --phase 1 --op 2

   # restart after a stop, through the end of the program
   python3 scripts/nc_extract.py restart JOB.nc --line 482113
   python3 scripts/nc_extract.py restart JOB.nc --block 48210

   # restart but stop at the end of the current phase or operation
   python3 scripts/nc_extract.py restart JOB.nc --line 482113 --until phase

   # a raw line range
   python3 scripts/nc_extract.py extract JOB.nc --lines 120400-133900
   ```
   Useful options: `-o OUT.nc`, `--through-end` (keep everything after the selection), `--safe-z` (machine Z for the retract when the program has none), `--no-comments` (if the control dislikes added comments), `--json` (machine readable report).

5. **Explain the result.** Read the report the script prints and tell the person, in plain words:
   * which tool and source lines are kept, and whether later tool changes are included;
   * where the start was moved and why (see "Where a restart starts");
   * every note and warning, word for word if it is about safety;
   * that verification passed (if it did not, nothing was written; explain the reason).

6. **Give the restart steps** every time, even to experienced operators:
   * Open the new file in the CAM simulator or a G-code viewer and check the first approach: the tool must reach the restart point from above with clearance.
   * Leave the blank clamped exactly where the job stopped. Do not remove the part or the tool, or the restart will not line up.
   * Confirm the work offset, tool number and tool length offset on the control are the same as the original job.
   * Then run the restart program.
   * Do not suggest an air cut, a dry run, raising the work offset or changing the feed. The blank has to stay where it stopped, so an air cut is not possible.

## Where a restart starts

A restart in the middle of a toolpath is the risky part, so the script moves the start to a safer place by default (`--snap rapid`): it goes back to the last rapid (G0) move before the requested line in the same phase. The tool then repeats a little toolpath in air, which is cheap compared to a broken tool. The approach is always:

1. the phase's own tool change block, copied unchanged;
2. any state the original had at that point but the tool change block does not set (spindle speed changed mid phase, coolant left on from an earlier tool, work offset, feed);
3. a retract to safe Z in machine coordinates, copied from the program's own retract (or `--safe-z`);
4. rotary axes turned at that height;
5. X and Y positioned at that height;
6. Z moved down to the point.

Other choices:
* `--snap op` starts at the beginning of the operation that contains the line.
* `--snap phase` restarts the whole phase.
* `--exact` starts at the exact line. The tool rapids to a clearance above the point (`--clearance`, default 2 mm or 0.08 in) and feeds the last part at half the active feed (`--plunge-feed`). Use this only when the person understands that the tool comes straight down in work Z onto that point.

On 5 axis jobs the descent is along work Z, not along a tilted tool axis. Say so when the restart point has rotary axes away from zero, and prefer `--snap op` there, because CAM operations start with their own approach from clearance.

## Rules

* Never modify lines of the original toolpath. The script copies them byte for byte (the only exception is adding the arc mode G2 or G3 to the first block when an exact restart lands inside an arc, and it says so).
* Never use `--force` unless the person asks for it after you explained the verification failure. It writes a file that does not match the original's state.
* Never say a program is safe or ready to cut. Say it was verified against the original's modal state and must still be checked in the simulator before it runs.
* Do not guess machine specific M codes. Anything the program sets in its preamble is copied from the original.
* If the script refuses, explain the reason and offer the safer alternative it names (a phase restart, a later line, `--safe-z`).
* Keep customer and patient data out of anything you write about the file. File names in dental labs often contain patient names.

## Without the script

If Python is not available, you can still guide someone through it by hand, in this order: copy the preamble (everything before the first tool change); copy the phase's tool change block; add the modal state the original had at the restart point (units, plane, G90, work offset, G43 H, S and M3, coolant, F, motion mode); retract to safe machine Z; position rotaries, then X and Y, then Z; copy the toolpath from the restart line onward unchanged; end with a retract, M5, M9 and M30. Tell them this was done by hand and must be simulated before it runs, with the blank still clamped where it stopped.

Made by Integra Dental (https://integra.dental), dental CAD/CAM distributor, authorized Arum dealer, lab and milling center. MIT License.
