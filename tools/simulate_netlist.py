#!/usr/bin/env python3
"""Zero-delay functional simulator for an extracted Sky130 netlist."""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import re
from typing import Any, Iterable, Optional


Logic = Optional[bool]


def logic_not(value: Logic) -> Logic:
    return None if value is None else not value


def logic_and(left: Logic, right: Logic) -> Logic:
    if left is False or right is False:
        return False
    if left is True and right is True:
        return True
    return None


def logic_or(left: Logic, right: Logic) -> Logic:
    if left is True or right is True:
        return True
    if left is False and right is False:
        return False
    return None


def logic_xor(left: Logic, right: Logic) -> Logic:
    if left is None or right is None:
        return None
    return left != right


class Expression:
    TOKEN = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*|[01!&|^()])")

    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = self.TOKEN.findall(source)
        self.position = 0
        self.tree = self.parse_or()
        if self.position != len(self.tokens):
            raise ValueError(f"Could not parse expression: {source}")

    def peek(self, token: str) -> bool:
        return self.position < len(self.tokens) and self.tokens[self.position] == token

    def take(self, token: str) -> bool:
        if self.peek(token):
            self.position += 1
            return True
        return False

    def parse_or(self) -> Any:
        result = self.parse_xor()
        while self.take("|"):
            result = ("|", result, self.parse_xor())
        return result

    def parse_xor(self) -> Any:
        result = self.parse_and()
        while self.take("^"):
            result = ("^", result, self.parse_and())
        return result

    def parse_and(self) -> Any:
        result = self.parse_unary()
        while self.take("&"):
            result = ("&", result, self.parse_unary())
        return result

    def parse_unary(self) -> Any:
        if self.take("!"):
            return ("!", self.parse_unary())
        if self.take("("):
            result = self.parse_or()
            if not self.take(")"):
                raise ValueError(f"Unclosed parenthesis in expression: {self.source}")
            return result
        if self.position >= len(self.tokens):
            raise ValueError(f"Unexpected end of expression: {self.source}")
        token = self.tokens[self.position]
        self.position += 1
        if token in {"0", "1"}:
            return token == "1"
        return ("variable", token)

    def evaluate(self, values: dict[str, Logic]) -> Logic:
        def visit(node: Any) -> Logic:
            if isinstance(node, bool):
                return node
            if node[0] == "variable":
                return values.get(node[1])
            if node[0] == "!":
                return logic_not(visit(node[1]))
            if node[0] == "&":
                return logic_and(visit(node[1]), visit(node[2]))
            if node[0] == "|":
                return logic_or(visit(node[1]), visit(node[2]))
            if node[0] == "^":
                return logic_xor(visit(node[1]), visit(node[2]))
            raise ValueError(f"Unknown expression node: {node}")

        return visit(self.tree)


class Simulator:
    def __init__(self, netlist: dict[str, Any]) -> None:
        self.netlist = netlist
        self.models = netlist["models"]
        self.instances = netlist["instances"]
        self.ports = netlist["ports"]
        self.inputs: dict[str, Logic] = {}
        self.state: dict[str, Logic] = {}
        self.values: dict[str, Logic] = {}

        self.sequential = []
        self.combinational = []
        self.expressions: dict[tuple[str, str], Expression] = {}
        self.async_expressions: dict[tuple[str, str], Expression] = {}
        for instance in self.instances:
            model = self.models[instance["cell"]]
            if model["sequential"]:
                self.sequential.append(instance)
                self.state[instance["name"]] = None
                metadata = next(iter(model["sequential"].values()))
                for condition in ("clear", "preset"):
                    key = (instance["cell"], condition)
                    if condition in metadata and key not in self.async_expressions:
                        self.async_expressions[key] = Expression(metadata[condition])
            else:
                self.combinational.append(instance)
                for pin, function in model["outputs"].items():
                    if function is None:
                        raise ValueError(f"No output function for {instance['cell']}.{pin}")
                    key = (instance["cell"], pin)
                    if key not in self.expressions:
                        self.expressions[key] = Expression(function)

        driver: dict[str, str] = {}
        by_name = {instance["name"]: instance for instance in self.combinational}
        for instance in self.combinational:
            model = self.models[instance["cell"]]
            for pin, net in instance["pins"].items():
                if model["pins"][pin] == "output":
                    driver[net] = instance["name"]
        dependencies: dict[str, set[str]] = {}
        consumers: dict[str, set[str]] = defaultdict(set)
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
        if len(order) != len(self.combinational):
            raise ValueError("Combinational network contains a loop")
        self.combinational_order = [by_name[name] for name in order]

    def set_inputs(self, **inputs: Logic) -> None:
        unknown = set(inputs) - set(self.ports)
        if unknown:
            raise ValueError(f"Unknown input ports: {sorted(unknown)}")
        self.inputs.update(inputs)
        self.settle(apply_async=True)

    def _evaluate_combinational(self) -> dict[str, Logic]:
        values: dict[str, Logic] = {
            self.ports[port]: value for port, value in self.inputs.items()
        }
        for instance in self.sequential:
            q_net = instance["pins"].get("Q")
            if q_net is not None:
                values[q_net] = self.state[instance["name"]]

        for instance in self.combinational_order:
            model = self.models[instance["cell"]]
            pin_values = {
                pin: values.get(net) for pin, net in instance["pins"].items()
            }
            for output_pin in model["outputs"]:
                values[instance["pins"][output_pin]] = self.expressions[
                    (instance["cell"], output_pin)
                ].evaluate(pin_values)
        return values

    def _async_value(self, instance: dict[str, Any]) -> Logic:
        model = self.models[instance["cell"]]
        metadata = next(iter(model["sequential"].values()))
        pin_values = {
            pin: self.values.get(net) for pin, net in instance["pins"].items()
        }
        if "clear" in metadata:
            clear = self.async_expressions[(instance["cell"], "clear")].evaluate(
                pin_values
            )
            if clear is True:
                return False
        if "preset" in metadata:
            preset = self.async_expressions[(instance["cell"], "preset")].evaluate(
                pin_values
            )
            if preset is True:
                return True
        return self.state[instance["name"]]

    def settle(self, apply_async: bool = False) -> None:
        for _ in range(4):
            self.values = self._evaluate_combinational()
            if not apply_async:
                return
            updates = {
                instance["name"]: self._async_value(instance)
                for instance in self.sequential
            }
            if all(self.state[name] == value for name, value in updates.items()):
                return
            self.state.update(updates)
        raise RuntimeError("Asynchronous sequential logic did not settle")

    def tick(self, clock_port: str = "clk") -> None:
        self.inputs[clock_port] = False
        self.settle(apply_async=True)
        clocks_before = {
            instance["name"]: self.values.get(instance["pins"]["CLK"])
            for instance in self.sequential
        }

        self.inputs[clock_port] = True
        self.settle(apply_async=True)
        next_state: dict[str, Logic] = {}
        for instance in self.sequential:
            name = instance["name"]
            clock_after = self.values.get(instance["pins"]["CLK"])
            if clocks_before[name] is False and clock_after is True:
                next_state[name] = self.values.get(instance["pins"]["D"])
            else:
                next_state[name] = self.state[name]
        self.state.update(next_state)
        self.settle(apply_async=True)

    def output(self, port: str) -> Logic:
        return self.values.get(self.ports[port])

    def output_byte(self, prefix: str = "O") -> int | None:
        bits = [self.output(f"{prefix}[{index}]") for index in range(8)]
        if any(bit is None for bit in bits):
            return None
        return sum(int(bit) << index for index, bit in enumerate(bits))


def bits_msb(value: int, width: int) -> Iterable[bool]:
    return (bool((value >> index) & 1) for index in reversed(range(width)))


def validate_warmup(netlist: dict[str, Any]) -> None:
    def run(left: int, right: int) -> Logic:
        simulator = Simulator(netlist)
        simulator.set_inputs(clk=False, rst_n=False, en=False, A=False, B=False)
        simulator.tick()
        simulator.set_inputs(rst_n=True, en=True)
        for left_bit, right_bit in zip(bits_msb(left, 8), bits_msb(right, 8)):
            simulator.set_inputs(A=left_bit, B=right_bit)
            simulator.tick()
        return simulator.output("S")

    cases = [(248, 248, True), (255, 241, True), (100, 100, False)]
    for left, right, expected in cases:
        actual = run(left, right)
        if actual is not expected:
            raise AssertionError(
                f"Warm-up failed for {left} + {right}: {actual} != {expected}"
            )
    print("Warm-up simulation passed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--validate-warmup", action="store_true")
    args = parser.parse_args()
    netlist = json.loads(args.netlist.read_text())
    if args.validate_warmup:
        validate_warmup(netlist)


if __name__ == "__main__":
    main()
