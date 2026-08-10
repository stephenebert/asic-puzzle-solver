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
make setup
make all
```

`make all` runs the fast core verification. The deeper, optional checks are:

```sh
make protocol architecture easter-eggs
make verify-independent
make media
```

The KLayout cross-check has a separate pinned dependency in
`requirements-klayout.txt`. On the development Mac, the locally built KLayout
wheel linked against Anaconda's existing native libraries, so the two targets
that use it require:

```sh
KLAYOUT_ENV='DYLD_LIBRARY_PATH=/opt/anaconda3/lib' make verify-independent
KLAYOUT_ENV='DYLD_LIBRARY_PATH=/opt/anaconda3/lib' make media
```

That workaround is specific to this local wheel; a healthy platform wheel does
not need it. No system symlinks or binary patches are used.

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
10. Repeat the physical extraction with KLayout's independent GDS parser and
    Region engine, then compare nets as endpoint partitions rather than by
    arbitrary generated names.
11. Prove every consecutive enabled burst shorter than 121 bits impossible,
    prove the 121-bit witness unique, and prove that longer suffix bits cannot
    affect `success`.
12. Classify every flip-flop by role and mechanically decode the VCD and
    physical-layout Easter eggs.

The real extraction contains 728 functional standard-cell instances and 739
signal nets. The only dangling load is in the isolated output-generator area;
all other one-ended nets are intentionally unused buffer or constant outputs.

## Why the answer is correct

The automated verifier checks several independent boundaries:

1. A fresh extraction from `puzzle.gds` exactly matches the netlist being
   tested: 728 functional standard-cell instances and 739 nets.
2. A concrete simulation matches all 312 rising edges in
   `example_inputs.vcd`, including the complete output byte and `success`.
3. A concrete replay of the recovered board raises `success` and emits
   `(* TWO STARS *)`.
4. A separate Star Battle constraint model finds the same board and proves a
   second board is impossible.
5. An SMT counterexample query proves that circuit acceptance and the direct
   row, column, region, and no-touch rules agree for all 2^121 input boards.
6. A second extractor, which shares neither the gdstk parser nor the Shapely
   geometry engine, independently recovers the same 728 functional standard-cell
   instances, 66 cell variants, 13 ports, and all 739 canonical net endpoint
   sets.
7. A separate protocol proof establishes that lengths 0 through 120 are
   unsatisfiable, length 121 has the unique stored witness, and the acceptance
   decision for every longer burst depends only on its first 121 bits.

The last result is exhaustive under the recovered standard-cell model. The
fresh extraction and supplied-waveform replay validate the boundary between
that model and the physical layout; this is not a transistor-level proof.

## Recovered sequential architecture

Every one of the 92 flip-flops has a concrete role:

- 44 flip-flops implement twenty-two two-bit counters: eleven column counters
  and eleven recovered-region counters.
- 9 track the serial position: four column-phase bits, four row-phase bits, and
  a terminal flag.
- 8 count the total number of stars.
- 3 implement the per-row check.
- 13 implement a twelve-bit sliding history and sticky no-touch violation.
- 15 implement verdict, success, character index, and output-byte state.

`tools/analyze_architecture.py` records the exact instance mapping, state
dependencies, and combinational cone sizes in `build/architecture.json`. The
604 non-clock combinational cells are also accounted for: 592 belong exclusively
to one logical group and 12 are shared.

## Exact clock-protocol theorem

Under the explicit protocol of three reset edges, an optional idle edge, `N`
consecutive enabled input edges, and then one disabled decision edge:

- every `N` from 0 through 120 is unsatisfiable;
- `N = 121` is satisfiable with exactly the stored 121-bit board;
- the optional idle edge is a no-op on the entire 79-flip-flop success cone;
- the disabled decision edge is independent of the value held on `I`;
- for every `N >= 121`, success depends only on the first 121 bits and the
  remaining suffix bits are don't-cares for acceptance.

If `enable` stays high, success can first rise on enabled edge 122. Extra
enabled clocks also advance the output generator, so `enable` should fall
immediately after bit 121 to capture the complete ASCII message from its first
character.

## Output diagnostics and Easter eggs

The automated Easter-egg verifier reproduces the VCD phrase
`The night sky awaits` and the 36-bar physical Morse message
`PER ARENAM AD ASTRA` (naturally read as “through the sand to the stars”). It
also proves a five-way output classification for both static values of the sole
undriven net, over all 2^121 boards:

- empty board: `EMPTY SKY`;
- full board: `BIG BANG`;
- valid Star Battle: `(* TWO STARS *)`;
- correct row/column/region counts but touching stars: an X-sensitive
  `TWO?NOT TOUC?` diagnostic affected by the undriven output-only net;
- every other board: `TRY AGAIN`.

The ambiguous diagnostic must not be silently normalized. With `n0653 = 0`, it
is `TWO"NOT TOUCH`; with `n0653 = 1`, it is `TWO NOT TOUCJ` followed by bytes
`02 10`. Three-valued simulation leaves the affected bits unknown. Physical
reconstruction confirms that `n0653` reaches only `O[1]` and `O[4]` and touches
two input pins but no driver, rail, or top-level label. The winning output is
fully known and does not depend on this net.

## Performance

On the development Mac, success-cone slicing reduced symbolic solve time from
about 6.0 seconds to 4.0 seconds. Bit-parallel region recovery reduced its wall
time from about 13 seconds to 0.5 seconds while producing a byte-identical
region artifact. The full core verification pass takes about 9 seconds and is
CPU-only. The independent KLayout extraction takes about 1.2 seconds. The
minimum-length proof takes about 76 seconds, while the universal five-way output
classification takes about 30 seconds.

## Generated figures and videos

After `make media`, `build/media/` contains four evidence figures, a 16:9
walkthrough poster, a 78-second captioned explainer, a 20-second answer reveal,
matching SRT caption files, decoded-frame contact sheets, and a machine-readable
validation report. The videos are silent H.264 at 1920×1080 and 24 fps. They use
only the supplied layout and locally reproduced proof artifacts; no stock media
or external artwork is included.
