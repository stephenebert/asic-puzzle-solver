#!/usr/bin/env python3
"""Recover the hidden Star Battle regions with bit-parallel simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from simulate_netlist import Expression, Simulator


BOARD_CELLS = 121


def evaluate_mask(expression: Expression, values: dict[str, int], all_mask: int) -> int:
    """Evaluate a Liberty expression over many simulations packed into bit lanes."""

    def visit(node: Any) -> int:
        if isinstance(node, bool):
            return all_mask if node else 0
        if node[0] == "variable":
            return values.get(node[1], 0)
        if node[0] == "!":
            return (~visit(node[1])) & all_mask
        if node[0] == "&":
            return visit(node[1]) & visit(node[2])
        if node[0] == "|":
            return visit(node[1]) | visit(node[2])
        if node[0] == "^":
            return visit(node[1]) ^ visit(node[2])
        raise ValueError(f"Unsupported expression node: {node}")

    return visit(expression.tree)


class BitParallelSimulator:
    """Run the baseline and every one-hot board in one packed simulation."""

    def __init__(self, netlist: dict, lane_count: int):
        scalar = Simulator(netlist)
        self.netlist = netlist
        self.models = scalar.models
        self.expressions = scalar.expressions
        self.sequential = scalar.sequential
        self.combinational_order = scalar.combinational_order
        self.all_mask = (1 << lane_count) - 1
        self.async_expressions = scalar.async_expressions

    def initial_state(self) -> dict[str, int]:
        state = {}
        for instance in self.sequential:
            family = self.models[instance["cell"]]["family"]
            state[instance["name"]] = self.all_mask if family == "dfstp" else 0
        return state

    def evaluate(self, state: dict[str, int], inputs: dict[str, int]) -> dict[str, int]:
        values: dict[str, int] = {}
        for port, net in self.netlist["ports"].items():
            if port in inputs:
                values[net] = inputs[port]

        for instance in self.sequential:
            values[instance["pins"]["Q"]] = state[instance["name"]]

        for instance in self.combinational_order:
            model = self.models[instance["cell"]]
            pin_values = {
                pin: values.get(net, 0) for pin, net in instance["pins"].items()
            }
            for output_pin in model["outputs"]:
                expression = self.expressions[(instance["cell"], output_pin)]
                output_net = instance["pins"][output_pin]
                values[output_net] = evaluate_mask(expression, pin_values, self.all_mask)
        return values

    def settle_async(
        self, state: dict[str, int], inputs: dict[str, int]
    ) -> tuple[dict[str, int], dict[str, int]]:
        for _ in range(4):
            values = self.evaluate(state, inputs)
            next_state = dict(state)
            for instance in self.sequential:
                metadata = next(iter(self.models[instance["cell"]]["sequential"].values()))
                pin_values = {
                    pin: values.get(net, 0) for pin, net in instance["pins"].items()
                }
                value = state[instance["name"]]
                remaining = self.all_mask
                if "clear" in metadata:
                    clear = evaluate_mask(
                        self.async_expressions[(instance["cell"], "clear")],
                        pin_values,
                        self.all_mask,
                    )
                    value &= ~clear
                    remaining &= ~clear
                if "preset" in metadata:
                    preset = evaluate_mask(
                        self.async_expressions[(instance["cell"], "preset")],
                        pin_values,
                        self.all_mask,
                    )
                    value = (value & ~preset) | (preset & remaining)
                next_state[instance["name"]] = value & self.all_mask
            if next_state == state:
                return state, values
            state = next_state
        raise RuntimeError("Asynchronous sequential logic did not settle")

    def tick(self, state: dict[str, int], inputs: dict[str, int]) -> dict[str, int]:
        low_inputs = dict(inputs)
        low_inputs["clk"] = 0
        state, before = self.settle_async(state, low_inputs)

        high_inputs = dict(inputs)
        high_inputs["clk"] = self.all_mask
        state, after = self.settle_async(state, high_inputs)

        next_state = dict(state)
        for instance in self.sequential:
            pins = instance["pins"]
            rising = (~before.get(pins["CLK"], 0)) & after.get(pins["CLK"], 0)
            rising &= self.all_mask
            old = state[instance["name"]]
            data = after.get(pins["D"], 0)
            next_state[instance["name"]] = (old & ~rising) | (data & rising)
        next_state, _ = self.settle_async(next_state, high_inputs)
        return next_state


def packed_final_state(netlist: dict) -> dict[str, int]:
    """Return final packed state; lane zero is the all-zero baseline."""
    lane_count = BOARD_CELLS + 1
    simulator = BitParallelSimulator(netlist, lane_count)
    state = simulator.initial_state()

    low = {"rst_n": 0, "enable": 0, "I": 0}
    high = {
        "rst_n": simulator.all_mask,
        "enable": 0,
        "I": 0,
    }
    for _ in range(3):
        state = simulator.tick(state, low)
    state = simulator.tick(state, high)

    active = dict(high)
    active["enable"] = simulator.all_mask
    for position in range(BOARD_CELLS):
        active["I"] = 1 << (position + 1)
        state = simulator.tick(state, active)
    return state


def recover_regions(netlist: dict) -> dict[str, Any]:
    counter_names = {
        instance["name"]
        for instance in netlist["instances"]
        if netlist["models"][instance["cell"]]["sequential"]
        and 100 < instance["x"] < 150
    }
    state = packed_final_state(netlist)
    baseline = {name: state[name] & 1 for name in counter_names}

    signatures: list[frozenset[str]] = []
    for position in range(BOARD_CELLS):
        lane = position + 1
        signatures.append(
            frozenset(
                name
                for name in counter_names
                if ((state[name] >> lane) & 1) != baseline[name]
            )
        )

    if any(len(difference) != 2 for difference in signatures):
        raise AssertionError("Each one-hot input should change two counter banks")

    column_markers = [
        next(
            iter(
                set.intersection(
                    *(set(signatures[row * 11 + column]) for row in range(11))
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
            marker_set = set(signatures[row * 11 + column]) - column_marker_set
            if len(marker_set) != 1:
                raise AssertionError(
                    f"Cell ({row}, {column}) has ambiguous region marker {marker_set}"
                )
            marker = next(iter(marker_set))
            region_labels.setdefault(marker, len(region_labels))
            region_row.append(region_labels[marker])
        regions.append(region_row)

    return {
        "column_counter_markers": column_markers,
        "region_counter_markers": region_labels,
        "regions": regions,
        "region_sizes": [
            sum(value == region for row in regions for value in row)
            for region in range(len(region_labels))
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--output", type=Path, default=Path("build/regions.json"))
    args = parser.parse_args()

    netlist = json.loads(args.netlist.read_text())
    payload = recover_regions(netlist)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")

    print(f"Recovered {len(payload['region_counter_markers'])} regions")
    for row in payload["regions"]:
        print(" ".join(chr(ord("A") + value) for value in row))


if __name__ == "__main__":
    main()
