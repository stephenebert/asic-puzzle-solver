# Jane Street ASIC Puzzle Solver

<p align="center">
  <img src="assets/jane_street_logo.svg" width="480" alt="Jane Street spiral artwork recovered from the puzzle GDS layout">
</p>

<p align="center"><em>Spiral artwork recovered from the metal-2 layer of the supplied GDS layout.</em></p>

This project extracts a standard-cell netlist from `puzzle.gds` and recovers
the unique serial input accepted by the circuit.

The final output is

```text
(* TWO STARS *)
```

The repository contains two solvers that recover the same 121-bit input.

- Star Battle backtracking without Z3
- Symbolic circuit solving with Z3

Both replay that input through the full 728-cell circuit.

## Setup

You need Python 3.9 or newer, Git, and Make.

```sh
git clone https://github.com/stephenebert/asic-puzzle-solver.git
cd asic-puzzle-solver
```

The first run downloads the required Python packages and Sky130 cell data.

## Star Battle solver

```sh
make solve-star-battle
make verify-star-battle
```

This method extracts the circuit, recovers the 11 x 11 region map, and solves
the board with ordinary backtracking. It exhausts the search to prove the board
is unique. It then replays the board through the full circuit.

## Z3 solver

```sh
make solve-z3
make verify-z3
```

This method symbolically executes the recovered circuit for 121 clocks. It
finds the unique successful input and checks it with a concrete replay.

The proof suite also checks all 312 rising edges in the supplied VCD. It proves
that the circuit and the Star Battle rules agree for all 2^121 boards.

## Compare both methods

```sh
make compare-solvers
```

This reruns both solvers and checks that their results match byte-for-byte.

## Tests and extended checks

```sh
make test
make verify-extended
```

`make test` builds the generated fixtures and runs the unit tests. The extended
target also runs the protocol analysis, architecture report, Easter egg checks,
and logo verification.

The KLayout extraction is an optional cross-check:

```sh
make verify-independent
```

Its Python wheel may need compatible native `libcurl` and `libpng` libraries on
recent macOS versions. `make verify-all` runs it after the extended suite.

## Main files

- `tools/extract_netlist.py` performs the primary GDS extraction
- `tools/extract_netlist_klayout.py` performs the KLayout cross-check
- `tools/recover_regions.py` recovers the irregular region partition
- `tools/solve_star_battle.py` performs exhaustive direct search
- `tools/solve_symbolic.py` performs the symbolic circuit solve
- `tools/prove_protocol.py` checks protocol length and suffix behavior
- `tools/verify_solution.py` runs the end-to-end consistency suite
- `tools/verify_logo.py` verifies and renders the floating metal-2 artwork
- `build/solution.json` and `build/regions.json` contain the tracked result

Full technical evidence is in [LOCAL_NOTES.md](LOCAL_NOTES.md).
