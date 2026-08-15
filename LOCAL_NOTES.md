# Jane Street ASIC puzzle notes

## Result

The circuit checks an 11 by 11 two star Star Battle board.

The rules require exactly two stars in every row, column, and region. No two
stars may touch horizontally, vertically, or diagonally.

The unique board is

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

The 121 bit serial input is

```text
0000000101010000100000000000010101010000000000001010000001000001000000100000101000010000000100000010000010010001010000000
```

The circuit raises `success` and emits

```text
(* TWO STARS *)
```

The recovered region map is stored in `build/regions.json`.

## Run

Use ordinary Star Battle backtracking

```sh
make solve-star-battle
make verify-star-battle
```

Use Z3 on the recovered circuit

```sh
make solve-z3
make verify-z3
```

Compare both results

```sh
make compare-solvers
```

Run deeper checks

```sh
make protocol
make architecture
make easter-eggs
make verify-independent
```

## Method

1. Preserve standard cell references from the GDS hierarchy.
2. Join conductor shapes from `li1` through `met5` with their via cuts.
3. Map cell pins and top level ports onto the connected shapes.
4. Read cell behavior from the official Sky130 Liberty data.
5. Check the extractor against the supplied warmup circuit.
6. Replay every rising edge in the supplied VCD.
7. Recover the region map with 121 one hot simulations packed into Python
   integer lanes.
8. Solve the recovered Star Battle with exhaustive row by row backtracking.
9. Solve the circuit again by unrolling 121 clocks with Z3.
10. Repeat extraction with KLayout and compare every recovered net.

The extracted design has 728 functional cells, 739 signal nets, and 92 flip
flops.

## Explanation

The result is checked in several independent ways.

- Fresh extraction recovers all 728 functional cells and 739 nets.
- Warmup simulation reproduces the known `A + B == 496` behavior.
- Concrete simulation matches all 312 rising edges in the supplied VCD.
- Backtracking exhausts the rule puzzle and finds exactly one board.
- Full circuit replay accepts that board and emits `(* TWO STARS *)`.
- Z3 finds the same input and proves there is no second accepted 121 bit input.
- A universal Z3 query proves that the circuit and the direct rules agree for
  all 2^121 boards.
- KLayout independently recovers the same cells, ports, and net endpoints.
- A protocol proof shows that no input shorter than 121 bits can succeed.

These claims are exhaustive under the recovered functional cell model. They
are not transistor level or physical timing proofs.

## Circuit structure

The 92 flip flops have the following roles.

- 22 track column counts
- 22 track region counts
- 9 track the serial position
- 8 count all stars
- 3 check each row
- 13 detect touching stars
- 15 control the result and ASCII output

The only undriven net is inside the output generator. It does not affect the
winning result.

## Input protocol

1. Hold `rst_n` low for three rising clock edges.
2. Release reset.
3. Set `enable` high.
4. Clock the 121 bits into `I` from left to right.
5. Set `enable` low.
6. Continue clocking to read one ASCII byte from `O` on each edge.

On the first disabled edge `success` rises and `O` contains `(`. Extra enabled
bits after the first 121 do not change acceptance. They do advance the output
generator and can hide the first characters.

## Easter eggs

- The two supplied inputs hide `The night sky awaits`.
- Morse bars below the die decode to `PER ARENAM AD ASTRA`.
- `(* TWO STARS *)` is also valid OCaml comment syntax.
- An empty board emits `EMPTY SKY`.
- A full board emits `BIG BANG`.
- Other ordinary failures emit `TRY AGAIN`.
- The VCD date points to the leap second at the end of 2016.
- The warmup target 496 is a perfect number.
