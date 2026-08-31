#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import re
import sys
from time import perf_counter
from typing import Any

import z3

from extract_netlist import extract
from simulate_netlist import Simulator
from solve_symbolic import SymbolicSimulator, recover_output


BOARD_SIZE = 11
BOARD_CELLS = BOARD_SIZE * BOARD_SIZE
STARS_PER_UNIT = 2
EXPECTED_MESSAGE = b"(* TWO STARS *)"
EXPECTED_VCD_EDGES = 312
EXPECTED_VCD_RUNS = [BOARD_CELLS, BOARD_CELLS]
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


class VerificationError(RuntimeError):
    """Raised when a proof or regression check fails."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def comparable_netlist(netlist: dict[str, Any]) -> dict[str, Any]:
    """Normalize provenance paths while preserving every extracted logic field."""
    comparable = {key: value for key, value in netlist.items() if key != "source"}
    comparable["models"] = {
        name: {
            **model,
            "liberty": Path(model["liberty"]).name,
        }
        for name, model in netlist["models"].items()
    }
    return comparable


def verify_extraction(
    stored: dict[str, Any], gds_path: Path, vendor_root: Path
) -> tuple[int, int]:
    fresh = extract(gds_path, vendor_root)
    require(
        comparable_netlist(fresh) == comparable_netlist(stored),
        "fresh GDS extraction differs from the netlist under test",
    )
    return fresh["stats"]["instances"], fresh["stats"]["nets"]


def parse_vcd(path: Path) -> tuple[dict[str, tuple[str, int]], dict[int, list[tuple[str, str]]]]:
    declarations: dict[str, tuple[str, int]] = {}
    events: dict[int, list[tuple[str, str]]] = defaultdict(list)
    current_time: int | None = None

    declaration = re.compile(r"^\$var\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)")
    scalar_change = re.compile(r"^([01xXzZ])(\S+)$")
    vector_change = re.compile(r"^[bB]([01xXzZ]+)\s+(\S+)$")

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        match = declaration.match(line)
        if match:
            width, symbol, reference = match.groups()
            declarations[reference] = (symbol, int(width))
            continue
        if line.startswith("#"):
            current_time = int(line[1:])
            events.setdefault(current_time, [])
            continue
        if current_time is None:
            continue
        match = vector_change.match(line) or scalar_change.match(line)
        if match:
            value, symbol = match.groups()
            events[current_time].append((symbol, value.lower()))

    return declarations, dict(events)


def vcd_logic(value: str) -> bool | None:
    if value == "0":
        return False
    if value == "1":
        return True
    return None


def verify_vcd_replay(netlist: dict[str, Any], vcd_path: Path) -> tuple[int, list[int]]:
    declarations, events = parse_vcd(vcd_path)
    required = {"clk", "rst_n", "enable", "I", "O", "success"}
    require(required <= set(declarations), "VCD is missing one or more puzzle signals")
    require(declarations["O"][1] == 8, "VCD output bus is not eight bits wide")

    symbols = {reference: declarations[reference][0] for reference in required}
    values = {symbol: "x" for symbol, _ in declarations.values()}
    simulator = Simulator(netlist)
    simulator.set_inputs(clk=False, rst_n=False, enable=False, I=False)

    rising_edges = 0
    active_run = 0
    active_runs: list[int] = []
    for timestamp, changes in sorted(events.items()):
        previous_clock = values[symbols["clk"]]
        for symbol, value in changes:
            values[symbol] = value
        current_clock = values[symbols["clk"]]
        if previous_clock == "1" or current_clock != "1":
            continue

        sampled_inputs = {
            name: vcd_logic(values[symbols[name]])
            for name in ("rst_n", "enable", "I")
        }
        require(
            all(value is not None for value in sampled_inputs.values()),
            f"VCD has an unknown input at time {timestamp}",
        )
        simulator.set_inputs(**sampled_inputs)
        simulator.tick()
        rising_edges += 1

        if sampled_inputs["enable"]:
            active_run += 1
        elif active_run:
            active_runs.append(active_run)
            active_run = 0

        expected_success = vcd_logic(values[symbols["success"]])
        output_value = values[symbols["O"]]
        expected_output = (
            None if any(bit in output_value for bit in "xz") else int(output_value, 2)
        )
        actual_success = simulator.output("success")
        actual_output = simulator.output_byte()
        require(
            actual_success == expected_success and actual_output == expected_output,
            (
                f"VCD mismatch at time {timestamp}: success "
                f"{actual_success!r}/{expected_success!r}, output "
                f"{actual_output!r}/{expected_output!r}"
            ),
        )

    if active_run:
        active_runs.append(active_run)
    require(
        rising_edges == EXPECTED_VCD_EDGES,
        f"expected {EXPECTED_VCD_EDGES} VCD rising edges, found {rising_edges}",
    )
    require(
        active_runs == EXPECTED_VCD_RUNS,
        f"expected VCD input runs {EXPECTED_VCD_RUNS}, found {active_runs}",
    )
    return rising_edges, active_runs


def candidate_bits(solution: dict[str, Any]) -> list[bool]:
    bit_string = solution.get("bits", "")
    require(
        len(bit_string) == BOARD_CELLS and set(bit_string) <= {"0", "1"},
        f"solution must contain exactly {BOARD_CELLS} binary digits",
    )
    bits = [bit == "1" for bit in bit_string]
    expected_rows = [
        "".join("#" if bit else "." for bit in bits[offset : offset + BOARD_SIZE])
        for offset in range(0, BOARD_CELLS, BOARD_SIZE)
    ]
    require(solution.get("rows") == expected_rows, "stored rows do not match stored bits")
    require(solution.get("unique") is True, "stored solution is not marked unique")
    require(
        solution.get("alternate_bits") is None,
        "stored solution contains an alternate board",
    )
    return bits


def verify_concrete_winner(
    netlist: dict[str, Any], solution: dict[str, Any], bits: list[bool]
) -> bytes:
    success, output = recover_output(netlist, bits)
    require(success, "winning board did not raise success during concrete replay")
    require(output == EXPECTED_MESSAGE, f"unexpected success message: {output!r}")
    require(solution.get("success") is True, "stored solution does not mark success true")
    require(solution.get("output_hex") == output.hex(), "stored output hex is stale")
    require(
        solution.get("output_text") == output.decode("ascii"),
        "stored output text is stale",
    )
    return output


def validate_regions(payload: dict[str, Any]) -> list[list[Any]]:
    regions = payload.get("regions")
    require(
        isinstance(regions, list)
        and len(regions) == BOARD_SIZE
        and all(isinstance(row, list) and len(row) == BOARD_SIZE for row in regions),
        f"region map must be {BOARD_SIZE} by {BOARD_SIZE}",
    )
    labels = {label for row in regions for label in row}
    require(len(labels) == BOARD_SIZE, f"expected {BOARD_SIZE} regions, found {len(labels)}")
    sizes = [sum(label == region for row in regions for label in row) for region in sorted(labels)]
    if "region_sizes" in payload:
        require(payload["region_sizes"] == sizes, "stored region sizes are stale")
    return regions


def puzzle_rules(input_bits: list[Any], regions: list[list[Any]]) -> Any:
    def cell(row: int, column: int) -> Any:
        return input_bits[row * BOARD_SIZE + column]

    clauses: list[Any] = []
    for row in range(BOARD_SIZE):
        clauses.append(
            z3.PbEq([(cell(row, column), 1) for column in range(BOARD_SIZE)], STARS_PER_UNIT)
        )
    for column in range(BOARD_SIZE):
        clauses.append(
            z3.PbEq([(cell(row, column), 1) for row in range(BOARD_SIZE)], STARS_PER_UNIT)
        )
    for region in sorted({label for row in regions for label in row}):
        clauses.append(
            z3.PbEq(
                [
                    (cell(row, column), 1)
                    for row in range(BOARD_SIZE)
                    for column in range(BOARD_SIZE)
                    if regions[row][column] == region
                ],
                STARS_PER_UNIT,
            )
        )

    forward_neighbors = ((0, 1), (1, -1), (1, 0), (1, 1))
    for row in range(BOARD_SIZE):
        for column in range(BOARD_SIZE):
            for row_delta, column_delta in forward_neighbors:
                other_row = row + row_delta
                other_column = column + column_delta
                if 0 <= other_row < BOARD_SIZE and 0 <= other_column < BOARD_SIZE:
                    clauses.append(
                        z3.Or(
                            z3.Not(cell(row, column)),
                            z3.Not(cell(other_row, other_column)),
                        )
                    )
    return z3.And(clauses)


def verify_direct_solution(
    rules: Any, variables: list[Any], expected_bits: list[bool]
) -> None:
    solver = z3.Solver()
    solver.add(rules)
    status = solver.check()
    require(status == z3.sat, f"direct puzzle solver returned {status}")
    model = solver.model()
    actual_bits = [
        z3.is_true(model.eval(variable, model_completion=True)) for variable in variables
    ]
    require(actual_bits == expected_bits, "direct puzzle solution differs from stored board")

    solver.add(
        z3.Or(
            [
                variable != z3.BoolVal(value)
                for variable, value in zip(variables, actual_bits)
            ]
        )
    )
    alternate_status = solver.check()
    require(
        alternate_status == z3.unsat,
        f"direct puzzle rules admit another board ({alternate_status})",
    )


def graph_reaches(graph: dict[str, set[str]], start: str, target: str) -> bool:
    pending = deque([start])
    seen = {start}
    while pending:
        current = pending.popleft()
        if current == target:
            return True
        for neighbor in graph.get(current, set()):
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    return False


def verify_symbolic_assumptions(netlist: dict[str, Any]) -> tuple[int, int]:
    models = netlist["models"]
    instances = netlist["instances"]
    ports = netlist["ports"]
    drivers: dict[str, tuple[dict[str, Any], str]] = {}
    graph: dict[str, set[str]] = defaultdict(set)

    for instance in instances:
        model = models[instance["cell"]]
        inputs = [
            net for pin, net in instance["pins"].items() if model["pins"][pin] == "input"
        ]
        outputs = [
            (pin, net)
            for pin, net in instance["pins"].items()
            if model["pins"][pin] == "output"
        ]
        for output_pin, output_net in outputs:
            require(output_net not in drivers, f"multiple drivers on net {output_net}")
            drivers[output_net] = (instance, output_pin)
        for input_net in inputs:
            graph[input_net].update(output_net for _, output_net in outputs)

    sequential = [instance for instance in instances if models[instance["cell"]]["sequential"]]
    supported_families = {"dfrtp", "dfstp", "dfxtp"}
    for instance in sequential:
        model = models[instance["cell"]]
        family = model["family"]
        require(family in supported_families, f"unsupported sequential family {family}")
        require(
            {"CLK", "D", "Q"} <= set(instance["pins"]),
            f"incomplete flip-flop {instance['name']}",
        )
        if family == "dfrtp":
            require(
                instance["pins"].get("RESET_B") == ports["rst_n"],
                f"nonstandard reset wiring on {instance['name']}",
            )
        elif family == "dfstp":
            require(
                instance["pins"].get("SET_B") == ports["rst_n"],
                f"nonstandard set wiring on {instance['name']}",
            )

        clock_net = instance["pins"]["CLK"]
        visited: set[str] = set()
        while clock_net != ports["clk"]:
            require(clock_net not in visited, f"clock loop feeding {instance['name']}")
            visited.add(clock_net)
            require(clock_net in drivers, f"undriven clock feeding {instance['name']}")
            clock_driver, _ = drivers[clock_net]
            clock_model = models[clock_driver["cell"]]
            clock_inputs = [
                net
                for pin, net in clock_driver["pins"].items()
                if clock_model["pins"][pin] == "input"
            ]
            require(
                not clock_model["sequential"]
                and clock_model["family"] == "clkbuf"
                and len(clock_inputs) == 1,
                f"unsupported clock path feeding {instance['name']}",
            )
            clock_net = clock_inputs[0]

    driven = set(drivers) | set(ports.values())
    floating = {
        net
        for net, endpoints in netlist["nets"].items()
        if net not in driven
        and any(endpoint.get("direction") == "input" for endpoint in endpoints)
    }
    success_net = ports["success"]
    for source in floating:
        require(
            not graph_reaches(graph, source, success_net),
            f"unconstrained source {source} can influence success",
        )

    reset_simulator = Simulator(netlist)
    reset_simulator.set_inputs(clk=False, rst_n=False, enable=False, I=False)
    for _ in range(3):
        reset_simulator.tick()
    unknown_state = [
        name for name, value in reset_simulator.state.items() if value is None
    ]
    require(
        not unknown_state,
        f"reset protocol leaves unknown flip-flop state: {unknown_state}",
    )
    return len(sequential), len(floating)


def circuit_acceptance(netlist: dict[str, Any], input_bits: list[Any]) -> Any:
    success_d_nets = [
        instance["pins"]["D"]
        for instance in netlist["instances"]
        if netlist["models"][instance["cell"]]["sequential"]
        and instance["pins"].get("Q") == netlist["ports"]["success"]
    ]
    require(len(success_d_nets) == 1, "success is not driven by exactly one flip-flop")
    success_d_net = success_d_nets[0]
    symbolic = SymbolicSimulator(netlist, {success_d_net})
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

    for bit in input_bits:
        active_inputs = {
            "clk": z3.BoolVal(True),
            "rst_n": z3.BoolVal(True),
            "enable": z3.BoolVal(True),
            "I": bit,
        }
        state, _ = symbolic.tick(state, active_inputs)
    final_values = symbolic.evaluate(state, active_inputs)
    return final_values[success_d_net]


def verify_universal_equivalence(circuit: Any, rules: Any) -> None:
    solver = z3.Solver()
    solver.add(z3.Xor(circuit, rules))
    status = solver.check()
    require(
        status == z3.unsat,
        f"circuit and direct rules are not universally equivalent ({status})",
    )


def timed_result(label: str, started: float) -> None:
    print(f"PASS {label} ({perf_counter() - started:.2f}s)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "netlist",
        nargs="?",
        type=Path,
        default=REPOSITORY_ROOT / "build/puzzle_netlist.json",
    )
    parser.add_argument(
        "--solution",
        type=Path,
        default=REPOSITORY_ROOT / "build/solution.json",
    )
    parser.add_argument(
        "--regions",
        type=Path,
        default=REPOSITORY_ROOT / "build/regions.json",
    )
    parser.add_argument(
        "--vcd",
        type=Path,
        default=REPOSITORY_ROOT / "example_inputs.vcd",
    )
    parser.add_argument(
        "--gds",
        type=Path,
        default=REPOSITORY_ROOT / "puzzle.gds",
    )
    parser.add_argument(
        "--vendor-root",
        type=Path,
        default=REPOSITORY_ROOT / "vendor/sky130_fd_sc_hd",
    )
    parser.add_argument("--skip-extraction", action="store_true")
    args = parser.parse_args()

    total_started = perf_counter()
    try:
        netlist = json.loads(args.netlist.read_text())
        solution = json.loads(args.solution.read_text())
        region_payload = json.loads(args.regions.read_text())

        if not args.skip_extraction:
            started = perf_counter()
            instances, nets = verify_extraction(netlist, args.gds, args.vendor_root)
            timed_result(f"fresh extraction matches ({instances} instances, {nets} nets)", started)

        started = perf_counter()
        edge_count, run_lengths = verify_vcd_replay(netlist, args.vcd)
        timed_result(
            f"waveform replay matches ({edge_count} edges, input runs {run_lengths})",
            started,
        )

        bits = candidate_bits(solution)
        started = perf_counter()
        message = verify_concrete_winner(netlist, solution, bits)
        timed_result(f"winning replay emits {message.decode('ascii')}", started)

        regions = validate_regions(region_payload)
        variables = [z3.Bool(f"verify_input_{index:03d}") for index in range(BOARD_CELLS)]
        rules = puzzle_rules(variables, regions)
        started = perf_counter()
        verify_direct_solution(rules, variables, bits)
        timed_result("direct rules have exactly the stored solution", started)

        started = perf_counter()
        sequential_count, floating_count = verify_symbolic_assumptions(netlist)
        circuit = circuit_acceptance(netlist, variables)
        verify_universal_equivalence(circuit, rules)
        timed_result(
            (
                f"circuit equals the direct rules for all 2^{BOARD_CELLS} boards "
                f"({sequential_count} flip-flops, {floating_count} isolated floating net)"
            ),
            started,
        )
    except (OSError, ValueError, KeyError, VerificationError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1

    print(f"VERIFIED unique winning board ({perf_counter() - total_started:.2f}s total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
