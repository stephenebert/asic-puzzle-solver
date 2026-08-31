#!/usr/bin/env python3
"""Symbolically execute the recovered circuit to find a successful input."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
from typing import Any

import z3

from simulate_netlist import Expression, Simulator


BOARD_SIZE = 11
BOARD_CELLS = BOARD_SIZE * BOARD_SIZE


def evaluate_expression(expression: Expression, values: dict[str, Any]) -> Any:
    def visit(node: Any) -> Any:
        if isinstance(node, bool):
            return z3.BoolVal(node)
        if node[0] == "variable":
            return values[node[1]]
        if node[0] == "!":
            return z3.Not(visit(node[1]))
        if node[0] == "&":
            return z3.And(visit(node[1]), visit(node[2]))
        if node[0] == "|":
            return z3.Or(visit(node[1]), visit(node[2]))
        if node[0] == "^":
            return z3.Xor(visit(node[1]), visit(node[2]))
        raise ValueError(f"unknown expression node: {node}")

    return visit(expression.tree)


class SymbolicSimulator:
    def __init__(self, netlist: dict[str, Any], target_nets: set[str]) -> None:
        self.netlist = netlist
        self.models = netlist["models"]
        self.instances = netlist["instances"]
        self.ports = netlist["ports"]
        self.by_name = {instance["name"]: instance for instance in self.instances}
        all_sequential = [
            instance
            for instance in self.instances
            if self.models[instance["cell"]]["sequential"]
        ]
        all_combinational = [
            instance
            for instance in self.instances
            if not self.models[instance["cell"]]["sequential"]
        ]

        combinational_driver: dict[str, dict[str, Any]] = {}
        for instance in all_combinational:
            model = self.models[instance["cell"]]
            for pin, net in instance["pins"].items():
                if model["pins"][pin] == "output":
                    combinational_driver[net] = instance
        sequential_driver = {
            instance["pins"]["Q"]: instance
            for instance in all_sequential
            if "Q" in instance["pins"]
        }

        needed_combinational: set[str] = set()
        needed_sequential: set[str] = set()
        pending_nets = list(target_nets)
        visited_nets: set[str] = set()
        while pending_nets:
            net = pending_nets.pop()
            if net in visited_nets:
                continue
            visited_nets.add(net)

            combinational_instance = combinational_driver.get(net)
            if combinational_instance is not None:
                needed_combinational.add(combinational_instance["name"])
                model = self.models[combinational_instance["cell"]]
                pending_nets.extend(
                    input_net
                    for pin, input_net in combinational_instance["pins"].items()
                    if model["pins"][pin] == "input"
                )
                continue

            sequential_instance = sequential_driver.get(net)
            if sequential_instance is not None:
                needed_sequential.add(sequential_instance["name"])
                pending_nets.append(sequential_instance["pins"]["D"])

        self.sequential = [
            instance
            for instance in all_sequential
            if instance["name"] in needed_sequential
        ]
        self.combinational = [
            instance
            for instance in all_combinational
            if instance["name"] in needed_combinational
        ]
        self.cone_stats = {
            "combinational": len(self.combinational),
            "combinational_total": len(all_combinational),
            "sequential": len(self.sequential),
            "sequential_total": len(all_sequential),
            "nets": len(visited_nets),
        }
        self.expressions: dict[tuple[str, str], Expression] = {}
        for instance in self.combinational:
            for pin, function in self.models[instance["cell"]]["outputs"].items():
                key = (instance["cell"], pin)
                if key not in self.expressions:
                    self.expressions[key] = Expression(function)

        driver: dict[str, str] = {}
        for instance in self.combinational:
            model = self.models[instance["cell"]]
            for pin, net in instance["pins"].items():
                if model["pins"][pin] == "output":
                    driver[net] = instance["name"]

        dependencies: dict[str, set[str]] = {}
        consumers: dict[str, set[str]] = defaultdict(set)
        combinational_names = {instance["name"] for instance in self.combinational}
        for instance in self.combinational:
            model = self.models[instance["cell"]]
            deps = {
                driver[net]
                for pin, net in instance["pins"].items()
                if model["pins"][pin] == "input" and net in driver
            }
            deps.discard(instance["name"])
            dependencies[instance["name"]] = deps
            for dependency in deps:
                consumers[dependency].add(instance["name"])

        ready = deque(sorted(name for name, deps in dependencies.items() if not deps))
        order = []
        while ready:
            name = ready.popleft()
            order.append(name)
            for consumer in sorted(consumers[name]):
                dependencies[consumer].discard(name)
                if not dependencies[consumer]:
                    ready.append(consumer)
        if set(order) != combinational_names:
            missing = sorted(combinational_names - set(order))
            raise ValueError(f"combinational loop or missing dependency: {missing}")
        self.order = [self.by_name[name] for name in order]

        driven = set(combinational_driver)
        driven.update(
            instance["pins"]["Q"] for instance in all_sequential
        )
        driven.update(self.ports.values())
        self.floating_nets = {
            net
            for net, endpoints in netlist["nets"].items()
            if net not in driven
            and any(endpoint.get("direction") == "input" for endpoint in endpoints)
        }

    def initial_state(self) -> dict[str, Any]:
        state = {}
        for instance in self.sequential:
            family = self.models[instance["cell"]]["family"]
            state[instance["name"]] = z3.BoolVal(family == "dfstp")
        return state

    def evaluate(
        self, state: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        values = {
            self.ports[port]: value
            for port, value in inputs.items()
            if port in self.ports
        }
        for net in self.floating_nets:
            values[net] = z3.BoolVal(False)
        for instance in self.sequential:
            values[instance["pins"]["Q"]] = state[instance["name"]]

        for instance in self.order:
            model = self.models[instance["cell"]]
            pin_values = {
                pin: values[net]
                for pin, net in instance["pins"].items()
                if model["pins"][pin] == "input"
            }
            for output_pin in model["outputs"]:
                values[instance["pins"][output_pin]] = evaluate_expression(
                    self.expressions[(instance["cell"], output_pin)], pin_values
                )
        return values

    def tick(
        self, state: dict[str, Any], inputs: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        values = self.evaluate(state, inputs)
        next_state = {}
        reset_active = z3.is_false(inputs["rst_n"])
        for instance in self.sequential:
            family = self.models[instance["cell"]]["family"]
            if reset_active and family == "dfrtp":
                next_state[instance["name"]] = z3.BoolVal(False)
            elif reset_active and family == "dfstp":
                next_state[instance["name"]] = z3.BoolVal(True)
            else:
                next_state[instance["name"]] = values[instance["pins"]["D"]]
        return next_state, values


def recover_output(netlist: dict[str, Any], bits: list[bool]) -> tuple[bool, bytes]:
    simulator = Simulator(netlist)
    simulator.set_inputs(clk=False, rst_n=False, enable=False, I=False)
    for _ in range(3):
        simulator.tick()
    simulator.set_inputs(rst_n=True)
    simulator.tick()
    simulator.set_inputs(enable=True)
    for bit in bits:
        simulator.set_inputs(I=bit)
        simulator.tick()
    simulator.set_inputs(enable=False)

    output = bytearray()
    success = False
    for _ in range(256):
        simulator.tick()
        success = success or simulator.output("success") is True
        byte = simulator.output_byte()
        if byte is None:
            raise ValueError("output contains unknown bits")
        if byte == 0:
            if output:
                break
        else:
            output.append(byte)
    else:
        raise RuntimeError("output generator did not terminate with a zero byte")
    return success, bytes(output)


def solve(netlist: dict[str, Any], bit_count: int) -> dict[str, Any]:
    if bit_count < 1:
        raise ValueError("bit_count must be positive")

    success_d_net = next(
        instance["pins"]["D"]
        for instance in netlist["instances"]
        if netlist["models"][instance["cell"]]["sequential"]
        and instance["pins"].get("Q") == netlist["ports"]["success"]
    )
    symbolic = SymbolicSimulator(netlist, {success_d_net})
    stats = symbolic.cone_stats
    print(
        "Success cone: "
        f"{stats['combinational']}/{stats['combinational_total']} combinational, "
        f"{stats['sequential']}/{stats['sequential_total']} sequential cells",
        flush=True,
    )
    state = symbolic.initial_state()
    reset_inputs = {
        "clk": z3.BoolVal(True),
        "rst_n": z3.BoolVal(False),
        "enable": z3.BoolVal(False),
        "I": z3.BoolVal(False),
    }
    for _ in range(3):
        state, _ = symbolic.tick(state, reset_inputs)

    idle_inputs = dict(reset_inputs)
    idle_inputs["rst_n"] = z3.BoolVal(True)
    state, _ = symbolic.tick(state, idle_inputs)

    input_bits = [z3.Bool(f"input_{index:03d}") for index in range(bit_count)]
    values = None
    for bit in input_bits:
        active_inputs = {
            "clk": z3.BoolVal(True),
            "rst_n": z3.BoolVal(True),
            "enable": z3.BoolVal(True),
            "I": bit,
        }
        state, values = symbolic.tick(state, active_inputs)

    assert values is not None
    final_values = symbolic.evaluate(state, active_inputs)
    solver = z3.Solver()
    solver.add(final_values[success_d_net])
    print("Solving success constraint", flush=True)
    status = solver.check()
    if status != z3.sat:
        raise RuntimeError(f"solver returned {status}")
    model = solver.model()
    concrete_bits = [z3.is_true(model.eval(bit, model_completion=True)) for bit in input_bits]
    solver.add(
        z3.Or(
            [
                bit != z3.BoolVal(value)
                for bit, value in zip(input_bits, concrete_bits)
            ]
        )
    )
    alternate_status = solver.check()
    alternate_bits = None
    if alternate_status == z3.sat:
        alternate_model = solver.model()
        alternate_bits = [
            z3.is_true(alternate_model.eval(bit, model_completion=True))
            for bit in input_bits
        ]
    success, output = recover_output(netlist, concrete_bits)
    if not success:
        raise AssertionError("symbolic solution did not raise success in concrete replay")
    return {
        "bits": "".join("1" if bit else "0" for bit in concrete_bits),
        "rows": (
            [
                "".join(
                    "#" if bit else "."
                    for bit in concrete_bits[offset : offset + BOARD_SIZE]
                )
                for offset in range(0, BOARD_CELLS, BOARD_SIZE)
            ]
            if bit_count == BOARD_CELLS
            else []
        ),
        "success": success,
        "unique": alternate_status == z3.unsat,
        "alternate_bits": (
            None
            if alternate_bits is None
            else "".join("1" if bit else "0" for bit in alternate_bits)
        ),
        "output_hex": output.hex(),
        "output_text": output.decode("ascii", errors="replace"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path, help="recovered netlist JSON")
    parser.add_argument("--bits", type=int, default=121, help="serial input length")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/solution.json"),
        help="solution JSON path",
    )
    args = parser.parse_args()
    netlist = json.loads(args.netlist.read_text())
    result = solve(netlist, args.bits)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
