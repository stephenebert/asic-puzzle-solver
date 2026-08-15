# Jane Street ASIC Puzzle Solver

This repository turns Jane Street's `puzzle.gds` layout into a functional
gate-level model and recovers the final answer:

```text
(* TWO STARS *)
```

There are two complete solving paths:

- an intuitive solver based on Star Battle rules and ordinary backtracking;
- a symbolic solver that unrolls the circuit with Z3.

Both recover the same unique 121-bit input and replay it through the full
728-cell circuit. Everything runs on a laptop CPU; no GPU is needed.

## Quick start: intuitive solver, no Z3

You need Python 3.9 or newer, Git, Make, and an internet connection on the
first run. From a fresh clone:

```sh
git clone --branch agent/optimize-solver \
  https://github.com/stephenebert/asic-puzzle-solver.git
cd asic-puzzle-solver
make solve-intuitive
```

This command creates a small `.venv-intuitive` environment whose only direct
requirements are `gdstk` and `Shapely`, downloads the official Sky130 data,
extracts both layouts, checks the warm-up, recovers the region map, and runs
the backtracking solver. The search and circuit replay themselves run with
Python's `-S` flag, so Python does not add installed site packages to the
search path.

Run the complete non-Z3 regression path with:

```sh
make verify-intuitive
```

After the layout has been extracted once, the solving step can also be run
directly:

```sh
python3 -S tools/solve_intuitive.py build/puzzle_netlist.json \
  --regions build/regions.json \
  --output build/solution_intuitive.json
```

A successful run prints the unique board, confirms `success: true`, and ends
with:

```text
Output:  (* TWO STARS *)
```

## How the intuitive method works

The recovered circuit is checking an 11×11 two-star Star Battle. A valid board
must contain exactly two stars in every row, column, and outlined region, and
no two stars may touch, including diagonally.

The non-Z3 path follows that structure directly:

1. `extract_netlist.py` reconstructs nets from the GDS conductors and vias.
2. `simulate_netlist.py` provides a small functional simulator for the Sky130
   cells and validates the extractor against the supplied warm-up.
3. `recover_regions.py` simulates an all-zero board and 121 one-hot boards in
   parallel Python integer lanes. Each one-hot board reveals its column counter
   and region counter, which reconstructs all eleven regions.
4. `solve_intuitive.py` generates the 45 possible ways to place two
   non-touching stars in one row, then builds the board one row at a time.
5. A partial board is discarded only when stars touch, a counter exceeds two,
   or the unfilled rows no longer have enough capacity to bring a column or
   region up to two.
6. The search continues after finding the first board. Exhausting every
   remaining branch without finding a second board proves that the recovered
   Star Battle has a unique solution.
7. The winning 121 bits are clocked through the complete extracted circuit,
   which raises `success` and emits `(* TWO STARS *)` one byte per clock.

On the development Mac, the exhaustive board search visits 8,989 states and
takes about 0.2 seconds. The direct solver plus concrete replay takes about one
second; `make solve-intuitive`, including fresh extraction and region recovery,
takes about five seconds.

## Z3 method

The circuit-first solver uses Z3 instead of recognizing the puzzle rules.
Install its environment and run it with:

```sh
make setup
make solve-z3
```

For the core symbolic verification suite:

```sh
make verify-z3
```

`make all` is kept as a short alias for this Z3-backed verification path.

The symbolic solver traces backward from `success`, keeps only cells that can
influence it, represents the 121 serial inputs as Boolean variables, and
unrolls the sequential circuit for 121 clocks. Z3 finds the accepting input;
a second query blocks that exact model and establishes that no other 121-bit
input is accepted. A concrete full-netlist replay then recovers the ASCII
answer.

The core verifier goes further than finding one input. It replays all 312
rising edges in the supplied VCD and asks Z3 for any board on which circuit
acceptance differs from the direct Star Battle rules. That counterexample query
is unsatisfiable, proving agreement for all 2^121 boards under the recovered
functional standard-cell model.

## Compare both solvers

To rerun both methods and require their JSON outputs to match byte-for-byte:

```sh
make compare-solvers
```

The two paths support different claims:

| Check | Intuitive path | Z3 path |
| --- | --- | --- |
| Recovers the same 121-bit board | Yes | Yes |
| Exhaustively proves the recovered Star Battle is unique | Yes | Yes |
| Replays the winner through all 728 cells | Yes | Yes |
| Recovers `(* TWO STARS *)` | Yes | Yes |
| Proves circuit/rule equivalence for every 121-bit board | No | Yes |
| Proves the exact accepted protocol lengths | No | Optional deep check |

The intuitive proof is deliberately simple and inspectable. The Z3 proof is
the stronger bridge back to every possible input of the recovered circuit.

## Useful Make targets

| Command | What it does |
| --- | --- |
| `make solve-intuitive` | Re-extract, recover regions, and solve without Z3 |
| `make verify-intuitive` | Run non-Z3 tests, uniqueness search, and circuit replay |
| `make solve-z3` | Rerun the success-cone symbolic solver |
| `make verify-z3` | Run the core VCD, uniqueness, replay, and equivalence checks |
| `make compare-solvers` | Require both solution artifacts to be identical |
| `make protocol` | Prove no shorter burst works and characterize longer bursts |
| `make architecture` | Classify all 92 flip-flops by their circuit role |
| `make easter-eggs` | Reproduce the VCD, output, and physical-layout Easter eggs |
| `make verify-independent` | Repeat extraction with KLayout and verify that netlist |
| `make verify-extended` | Run both methods and all optional proof stages |
| `make media` | Render the evidence figures and walkthrough videos |

Typical development-Mac timings are roughly 0.2 seconds for the intuitive
search, 4 seconds for the symbolic solve, 9 seconds for core verification, and
76 seconds for the full minimum-length protocol proof. Timings vary by machine.

## Generated artifacts

| Path | Contents |
| --- | --- |
| `build/puzzle_netlist.json` | Primary GDS extraction |
| `build/regions.json` | Recovered 11×11 region map |
| `build/solution_intuitive.json` | Backtracking solution and circuit output |
| `build/solution.json` | Z3 solution and checked-in reference result |
| `build/protocol_proof.json` | Accepted-length and suffix theorem |
| `build/architecture.json` | Sequential architecture classification |
| `build/easter_eggs.json` | Machine-checked hidden messages and output cases |
| `build/klayout_crosscheck.json` | Independent extractor comparison |
| `build/media/` | Figures, videos, captions, and visual QA artifacts |

Generated netlists, downloaded dependencies, and media remain local and are
excluded from version control. The compact region and reference-solution JSON
files are checked in so results are easy to compare.

## Optional KLayout cross-check

KLayout is isolated in `requirements-klayout.txt`; neither ordinary solver
requires it. A normal platform wheel should work with:

```sh
make verify-independent
```

The locally built wheel on the development Mac linked against existing
Anaconda native libraries. Only on that machine, use:

```sh
KLAYOUT_ENV='DYLD_LIBRARY_PATH=/opt/anaconda3/lib' make verify-independent
KLAYOUT_ENV='DYLD_LIBRARY_PATH=/opt/anaconda3/lib' make verify-extended
KLAYOUT_ENV='DYLD_LIBRARY_PATH=/opt/anaconda3/lib' make media
```

No system symlinks or binary patches are required.

## Challenge files

Jane Street's [challenge post](https://blog.janestreet.com/can-you-reverse-engineer-an-asic/)
describes the original task. The main supplied files are:

- `puzzle.gds`: the physical ASIC layout;
- `example_inputs.vcd`: two deliberately unsuccessful input traces;
- `layout.png`: a high-level map of the chip;
- `warmup/`: a smaller example with source, netlists, DEF, and GDS files.

The warm-up contains two shift registers, an adder, and a comparator checking
whether `A + B == 496`. It provides a useful known-answer test before touching
the unknown puzzle.

![Annotated puzzle layout](layout.png)

For the exact recovered board, proof boundaries, architecture, protocol, and
Easter eggs, see [`LOCAL_NOTES.md`](LOCAL_NOTES.md).
