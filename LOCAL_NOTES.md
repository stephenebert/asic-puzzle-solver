# Jane Street ASIC puzzle — local workbench notes

This directory is an analysis workspace. Nothing here submits an answer or
publishes the solution to a public repository.

## Result

The ASIC validates an 11×11 **two-star Star Battle** board. It consumes the
board in row-major order as 121 serial bits. A valid board has exactly two
stars in every row, column, and outlined region, with no stars touching even
diagonally.

The accepted board is unique:

```text
.......#.#.
#....#.....
.......#.#.
#.#........
....#.#....
..#.....#..
....#.....#
.#....#....
...#......#
.....#..#..
.#.#.......
```

The serial input string is:

```text
0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000
```

Concrete replay raises `success` and emits:

```text
(* TWO STARS *)
```

The recovered regions (`A` through `K`) and stars (`*`) are:

```text
A  A  A  A  A  B  B  C* D  D* E
A* A  F  A  A  B* C  C  D  D  E
A  A  F  B  B  B  B  C* C  D* E
A* A  F* B  G  G  G  E  C  C  E
F  A  F  B  G* E  E* E  E  E  E
F  F  F* B  G  G  G  E  H* H  H
B  B  B  B  B* B  G  E  H  I  I*
B  J* J  J  G  G  G* E  H  I  I
B  J  J  K* E  E  E  E  H  I  I*
B  B  J  K  K  E* E  E  H* H  H
B  J* J  K* E  E  E  E  E  E  E
```

## Reproduce locally

Run these commands from this directory:

```sh
.venv/bin/python tools/extract_netlist.py warmup/04_final.gds build/warmup_netlist.json
.venv/bin/python tools/extract_netlist.py puzzle.gds build/puzzle_netlist.json
.venv/bin/python tools/simulate_netlist.py build/warmup_netlist.json --validate-warmup
.venv/bin/python tools/solve_symbolic.py build/puzzle_netlist.json
.venv/bin/python tools/recover_regions.py build/puzzle_netlist.json
.venv/bin/python tools/verify_solution.py
```

## Method summary

1. Preserve the standard-cell references from the GDS hierarchy.
2. Union conductor geometry on `li1` through `met5` (GDS layers 67–72).
3. Join adjacent conductor layers wherever datatype-44 via cuts overlap.
4. Map transformed standard-cell pin labels and top-level I/O labels onto the
   resulting connected components.
5. Read Boolean functions and sequential metadata from the official Sky130
   Liberty JSON models.
6. Validate the method against the warm-up: it recovers all 84 DEF nets, has
   no dangling loads or multiple drivers, and reproduces `A + B == 496`.
7. Replay both supplied 121-bit examples against the real recovered netlist;
   both emit `TRY AGAIN` exactly as in `example_inputs.vcd`.
8. Slice the netlist to the cone that can influence `success`, symbolically
   unroll 121 clocks, and ask Z3 for the input that makes the `success`
   flip-flop's D input true. Block that model and solve again to establish
   uniqueness.
9. Recover the region map with 121 one-hot boards packed into parallel integer
   bit lanes. Each board changes one column-counter bank and one region-counter
   bank; grouping the latter identifies the 11 outlined regions.

The real extraction contains 728 functional standard-cell instances and 739
signal nets. The only dangling load is in the isolated output-generator area;
all other one-ended nets are intentionally unused buffer or constant outputs.

## Why the answer is correct

The automated verifier checks several independent boundaries:

1. A fresh extraction from `puzzle.gds` exactly matches the netlist being
   tested: 728 instances and 739 nets.
2. A concrete simulation matches all 312 rising edges in
   `example_inputs.vcd`, including the complete output byte and `success`.
3. A concrete replay of the recovered board raises `success` and emits
   `(* TWO STARS *)`.
4. A separate Star Battle constraint model finds the same board and proves a
   second board is impossible.
5. An SMT counterexample query proves that circuit acceptance and the direct
   row, column, region, and no-touch rules agree for all 2^121 input boards.

The last result is exhaustive under the recovered standard-cell model. The
fresh extraction and supplied-waveform replay validate the boundary between
that model and the physical layout; this is not a transistor-level proof.

## Performance

On the development Mac, success-cone slicing reduced symbolic solve time from
about 6.0 seconds to 4.0 seconds. Bit-parallel region recovery reduced its wall
time from about 13 seconds to 0.5 seconds while producing a byte-identical
region artifact. The full exhaustive verification pass takes about 9 seconds
and is CPU-only.
