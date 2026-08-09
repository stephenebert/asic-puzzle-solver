#!/usr/bin/env python3
"""Recover the 11 Star Battle regions with one-hot circuit experiments."""

from __future__ import annotations

import argparse
from functools import partial
import json
import multiprocessing
from pathlib import Path
from typing import Any

from simulate_netlist import Simulator


def final_state(netlist: dict[str, Any], one_hot_position: int) -> dict[str, Any]:
    simulator = Simulator(netlist)
    simulator.set_inputs(clk=False, rst_n=False, enable=False, I=False)
    for _ in range(3):
        simulator.tick()
    simulator.set_inputs(rst_n=True)
    simulator.tick()
    simulator.set_inputs(enable=True)
    for position in range(121):
        simulator.set_inputs(I=position == one_hot_position)
        simulator.tick()
    return simulator.state


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--output", type=Path, default=Path("build/regions.json"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    netlist = json.loads(args.netlist.read_text())

    counter_flops = {
        instance["name"]
        for instance in netlist["instances"]
        if netlist["models"][instance["cell"]]["sequential"]
        and 100 < instance["x"] < 150
    }
    baseline = final_state(netlist, -1)
    worker = partial(final_state, netlist)
    with multiprocessing.Pool(args.workers) as pool:
        states = pool.map(worker, range(121))
    differences = [
        {name for name in counter_flops if state[name] != baseline[name]}
        for state in states
    ]
    if any(len(difference) != 2 for difference in differences):
        raise AssertionError("Each one-hot input should change two counter banks")

    column_markers = [
        next(
            iter(
                set.intersection(
                    *(differences[row * 11 + column] for row in range(11))
                )
            )
        )
        for column in range(11)
    ]
    column_marker_set = set(column_markers)
    region_labels: dict[str, int] = {}
    regions = []
    for row in range(11):
        region_row = []
        for column in range(11):
            marker_set = differences[row * 11 + column] - column_marker_set
            if len(marker_set) != 1:
                raise AssertionError(
                    f"Cell ({row}, {column}) has ambiguous region marker {marker_set}"
                )
            marker = next(iter(marker_set))
            region_labels.setdefault(marker, len(region_labels))
            region_row.append(region_labels[marker])
        regions.append(region_row)

    result = {
        "column_counter_markers": column_markers,
        "region_counter_markers": region_labels,
        "regions": regions,
        "region_sizes": [
            sum(value == region for row in regions for value in row)
            for region in range(len(region_labels))
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for row in regions:
        print(" ".join(chr(ord("A") + value) for value in row))


if __name__ == "__main__":
    main()
