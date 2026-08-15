# Jane Street ASIC Puzzle Solver

This project rebuilds a logical circuit from `puzzle.gds` and finds the unique
input that makes the chip succeed.

The final output is

```text
(* TWO STARS *)
```

It includes two independent methods.

- Star Battle backtracking without Z3
- Symbolic circuit solving with Z3

Both methods recover the same 121 bit input and replay it through the full 728
cell circuit.

No GPU is needed.

## Setup

You need Python 3.9 or newer, Git, Make, and the GitHub CLI.

```sh
gh repo clone stephenebert/asic-puzzle-solver
cd asic-puzzle-solver
```

The first run downloads the required Python packages and Sky130 cell data.

## Star Battle solver

```sh
make solve-star-battle
make verify-star-battle
```

This method extracts the circuit, recovers the 11 by 11 region map, and solves
the board with ordinary backtracking. It exhausts the search to prove the board
is unique. It then replays the board through the full circuit.

The search takes about 0.2 seconds on the development Mac.

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

This reruns both solvers and checks that their results match byte for byte.

## Main files

- `tools/solve_star_battle.py` contains the backtracking solver
- `tools/solve_symbolic.py` contains the Z3 solver
- `tools/extract_netlist.py` rebuilds the circuit from the GDS layout
- `tools/simulate_netlist.py` runs the recovered circuit
- `build/solution.json` contains the final board and bitstream

Full technical evidence is in [LOCAL_NOTES.md](LOCAL_NOTES.md).
