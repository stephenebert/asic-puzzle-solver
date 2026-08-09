# ASIC Reverse-Engineering Puzzle

## Solver workbench

This repository includes a geometry-based GDS netlist extractor, a functional
Sky130 simulator, a symbolic solver, and a tool that reconstructs the puzzle's
region map. The workflow is CPU-only and reproduces the supplied warm-up before
solving the full circuit.

```sh
make setup
make all
```

The verified analysis and recovered result are documented in
[`LOCAL_NOTES.md`](LOCAL_NOTES.md). Generated netlists and downloaded
dependencies stay local and are excluded from version control.

`make all` ends with an exhaustive verification pass. It re-extracts the
puzzle, replays every rising edge in the supplied VCD, concretely replays the
winning input, proves the board is unique under the recovered rules, and asks
Z3 for a counterexample where the circuit and those rules disagree. That final
query is unsatisfiable across all 2^121 possible boards.

The two expensive analysis stages are optimized for a laptop CPU. Symbolic
execution is sliced to the cells that can influence `success`, and all 121
one-hot region experiments run simultaneously as bit lanes inside Python
integers. No GPU is required.

This repository provides the files for the Jane Street ASIC reverse-engineering puzzle! See the [blog post](https://blog.janestreet.com/can-you-reverse-engineer-an-asic/) for more details.

### Puzzle GDS

The puzzle GDS is in this repository, in the file named `puzzle.gds`. You can preview it using [KLayout](https://www.klayout.de/) or the [TinyTapeout Online GDS Viewer](https://gds-viewer.tinytapeout.com/).

See `example_inputs.vcd` which shows some inputs being fed to the design (unfortunately, not the correct inputs to make `success` go high!). You can view it using [Surfer](https://surfer-project.org/) or a similar tool.

To help you get started, below is an image with some hints. The region labelled as "output generator" is safe to ignore during your initial reverse-engineering steps, but you'll need to simulate it to get your final answer!

![](layout.png)

### Warm-up Puzzle

To familiarize yourself with the flow and help develop your tools, we've put together a small example design and run it through a very similar flow to the one used for the real thing! The example design consists of two shift registers, an adder, and a comparator, outputting success if `A + B == 496`.

You'll find the following files related to the warm-up puzzle:

- `warmup/00_source.v`: The original Verilog source code of the example design
- `warmup/01_netlist.v`: Synthesized netlist comprising of a list of standard cells
  and connections
- `warmup/02_netlist_with_power_rails.v`: Netlist with VDD and GND rails added
- `warmup/03_post_place_and_route.def`: Physical layout of cells and routing
  connections, corresponding to cell and net names.
- `warmup/04_final.gds`: The final manufacturable layout file, with many internal names
  removed
