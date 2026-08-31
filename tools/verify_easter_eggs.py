#!/usr/bin/env python3
"""Reproduce the VCD/Morse Easter eggs and characterize output messages.

The output-generator report deliberately preserves the sole undriven net as
three-valued data.  It never silently turns an X into the more readable phrase
that a human might expect.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
import json
from pathlib import Path
import re
from time import perf_counter
from typing import Any

import gdstk
from shapely.geometry import Point
import z3

from extract_netlist import (
    PIN_TEXTTYPE,
    POWER_PINS,
    connect_vias,
    locate,
    unioned_conductors,
)
from simulate_netlist import Simulator
from solve_symbolic import SymbolicSimulator


ROOT = Path(__file__).resolve().parent.parent
BOARD_SIZE = 11
TOUCHING_BOARD_WITNESS = (
    "000000100100000101000000000001010001000010001100000000000001100000"
    "0000000010110000000001000101000000010000010001010000000"
)
EXPECTED_VCD = "The night sky awaits  "
EXPECTED_MORSE = "PER ARENAM AD ASTRA"
MORSE = {
    ".-": "A", "-...": "B", "-.-.": "C", "-..": "D", ".": "E",
    "..-.": "F", "--.": "G", "....": "H", "..": "I", ".---": "J",
    "-.-": "K", ".-..": "L", "--": "M", "-.": "N", "---": "O",
    ".--.": "P", "--.-": "Q", ".-.": "R", "...": "S", "-": "T",
    "..-": "U", "...-": "V", ".--": "W", "-..-": "X", "-.--": "Y", "--..": "Z",
}


def decode_vcd(path: Path) -> dict[str, Any]:
    declarations: dict[str, tuple[str, int]] = {}
    events: dict[int, list[tuple[str, str]]] = defaultdict(list)
    now: int | None = None
    decl = re.compile(r"^\$var\s+\S+\s+(\d+)\s+(\S+)\s+(\S+)")
    change = re.compile(r"^([01xXzZ])(\S+)$")
    for raw in path.read_text().splitlines():
        line = raw.strip()
        match = decl.match(line)
        if match:
            width, symbol, reference = match.groups()
            declarations[reference] = (symbol, int(width))
        elif line.startswith("#"):
            now = int(line[1:])
        elif now is not None and (match := change.match(line)):
            value, symbol = match.groups()
            events[now].append((symbol, value))
    symbols = {name: declarations[name][0] for name in ("clk", "enable", "I")}
    values = {symbol: "x" for symbol, _ in declarations.values()}
    runs: list[list[str]] = []
    active: list[str] = []
    for _, changes in sorted(events.items()):
        old_clock = values[symbols["clk"]]
        for symbol, value in changes:
            values[symbol] = value.lower()
        if old_clock == "1" or values[symbols["clk"]] != "1":
            continue
        if values[symbols["enable"]] == "1":
            active.append(values[symbols["I"]])
        elif active:
            runs.append(active)
            active = []
    if active:
        runs.append(active)
    decoded_runs = []
    for bits in runs:
        if len(bits) != 121:
            raise AssertionError(f"Expected a 121-bit VCD run, got {len(bits)}")
        chunks = []
        for offset in range(0, 121, 11):
            chunk = bits[offset : offset + 11]
            character = chr(int("".join(reversed(chunk[:7])), 2))
            chunks.append({"bits7_lsb_first": "".join(chunk[:7]), "padding4": "".join(chunk[7:]), "character": character})
        decoded_runs.append({"bit_count": len(bits), "bits": "".join(bits), "text": "".join(item["character"] for item in chunks), "chunks": chunks})
    phrase = "".join(item["text"] for item in decoded_runs)
    if phrase != EXPECTED_VCD:
        raise AssertionError(f"Unexpected VCD phrase {phrase!r}")
    return {"phrase": phrase.rstrip(), "phrase_with_padding": phrase, "runs": decoded_runs, "confidence": "mechanically decoded and asserted"}


def decode_morse(
    gds_path: Path, netlist: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    library = gdstk.read_gds(gds_path)
    top = library.top_level()[0]
    bars = []
    for reference in top.references:
        bounds = reference.bounding_box()
        if reference.cell_name in {"INTERNAL_3", "INTERNAL_7"} and bounds[1][1] <= -50.0 + 1e-9:
            bars.append((bounds[0][0], bounds[1][0], reference.cell_name, bounds[0][1], bounds[1][1]))
    bars.sort()
    if len(bars) != 36:
        raise AssertionError(f"Expected 36 sub-die Morse bars, got {len(bars)}")
    unit = min(x1 - x0 for x0, x1, *_ in bars)
    symbols, current, letters, words = [], "", [], []
    for index, (x0, x1, cell_name, y0, y1) in enumerate(bars):
        width_units = round((x1 - x0) / unit)
        mark = "." if width_units == 1 else "-" if width_units == 3 else None
        if mark is None:
            raise AssertionError(f"Unexpected Morse bar width {x1-x0}")
        gap_units = None if index + 1 == len(bars) else round((bars[index + 1][0] - x1) / unit)
        if gap_units not in {None, 1, 3, 7}:
            raise AssertionError(f"Unexpected Morse gap {gap_units}")
        symbols.append({"index": index, "cell_name": cell_name, "x0_um": x0, "x1_um": x1, "y0_um": y0, "y1_um": y1, "width_um": x1-x0, "mark": mark, "gap_units": gap_units})
        current += mark
        if gap_units in {None, 3, 7}:
            letters.append({"morse": current, "letter": MORSE[current]})
            current = ""
        if gap_units in {None, 7}:
            words.append(letters)
            letters = []
    phrase = " ".join("".join(item["letter"] for item in word) for word in words)
    if phrase != EXPECTED_MORSE:
        raise AssertionError(f"Unexpected GDS Morse phrase {phrase!r}")
    morse = {"phrase": phrase, "unit_um": unit, "symbols": symbols, "words": words, "confidence": "mechanically decoded from 36 placed rectangles and asserted"}

    # Reconstruct the conductive component for n0653 and check whether it is
    # secretly aliased to a rail, driver, or top-level constant.  Signal-pin
    # labels are on li1 in this design, while power labels are on met1, so the
    # audit must inspect labels on every conductor layer rather than layer 67
    # alone.
    dangling_endpoints = netlist["nets"].get("n0653", [])
    if len(dangling_endpoints) != 2 or any(
        endpoint.get("direction") != "input"
        or endpoint.get("pin") != "A1"
        or "instance" not in endpoint
        for endpoint in dangling_endpoints
    ):
        raise AssertionError(f"Unexpected logical endpoints for n0653: {dangling_endpoints}")
    instances_by_name = {item["name"]: item for item in netlist["instances"]}
    expected_geometry_endpoints = {
        (instances_by_name[endpoint["instance"]]["reference_index"], endpoint["pin"])
        for endpoint in dangling_endpoints
    }

    layers = unioned_conductors(top)
    connected = connect_vias(top, layers)
    first_endpoint = dangling_endpoints[0]
    first_instance = instances_by_name[first_endpoint["instance"]]
    first_reference = top.references[first_instance["reference_index"]]
    target_roots = {
        connected.find((item.layer, component))
        for item in first_reference.get_labels()
        if item.text == first_endpoint["pin"]
        and item.texttype == PIN_TEXTTYPE
        and item.layer in layers
        for component in locate(layers, item.layer, Point(item.origin))
    }
    if len(target_roots) != 1:
        raise AssertionError(f"Could not identify one physical root for n0653: {target_roots}")
    target = next(iter(target_roots))

    label_hits = []
    for reference_index, reference in enumerate(top.references):
        for item in reference.get_labels():
            if item.texttype != PIN_TEXTTYPE or item.layer not in layers:
                continue
            roots = {
                connected.find((item.layer, component))
                for component in locate(layers, item.layer, Point(item.origin))
            }
            if target in roots:
                label_hits.append(
                    {
                        "reference_index": reference_index,
                        "cell": reference.cell_name,
                        "pin": item.text,
                        "layer": item.layer,
                        "x": item.origin[0],
                        "y": item.origin[1],
                    }
                )
    top_hits = []
    for item in top.labels:
        if item.layer not in layers:
            continue
        roots = {connected.find((item.layer, component)) for component in locate(layers, item.layer, Point(item.origin))}
        if target in roots:
            top_hits.append({"text": item.text, "layer": item.layer})

    power_hits = [item for item in label_hits if item["pin"] in POWER_PINS]
    signal_geometry_endpoints = {
        (item["reference_index"], item["pin"])
        for item in label_hits
        if item["pin"] not in POWER_PINS
    }
    logical_endpoint_audit = []
    for endpoint in dangling_endpoints:
        instance = instances_by_name[endpoint["instance"]]
        direction = netlist["models"][instance["cell"]]["pins"][endpoint["pin"]]
        logical_endpoint_audit.append(
            {
                "instance": endpoint["instance"],
                "reference_index": instance["reference_index"],
                "cell": instance["cell"],
                "pin": endpoint["pin"],
                "direction": direction,
            }
        )
        if direction != "input" or endpoint["pin"] != "A1":
            raise AssertionError(f"n0653 endpoint is not an input A1 pin: {endpoint}")

    physical = {
        "root": list(target),
        "conductor_layers_scanned": list(layers),
        "pin_label_hits": label_hits,
        "top_label_hits": top_hits,
        "power_label_hits": power_hits,
        "power_alias_found": bool(power_hits),
        "logical_endpoint_audit": logical_endpoint_audit,
        "conclusion": (
            "the component touches exactly two logical input A1 pins; no output "
            "driver, power-rail label, or top-level label was found on layers 67-72"
        ),
    }
    if (
        power_hits
        or top_hits
        or signal_geometry_endpoints != expected_geometry_endpoints
    ):
        raise AssertionError("n0653 physical endpoint audit changed")
    return morse, physical


def replay(netlist: dict[str, Any], bits: str, floating: bool | None) -> list[dict[str, int]]:
    copied = json.loads(json.dumps(netlist))
    if floating is not None:
        copied["ports"]["floating_probe"] = "n0653"
    simulator = Simulator(copied)
    inputs = {"clk": False, "rst_n": False, "enable": False, "I": False}
    if floating is not None:
        inputs["floating_probe"] = floating
    simulator.set_inputs(**inputs)
    for _ in range(3):
        simulator.tick()
    simulator.set_inputs(rst_n=True)
    simulator.tick()
    simulator.set_inputs(enable=True)
    for bit in bits:
        simulator.set_inputs(I=bit == "1")
        simulator.tick()
    simulator.set_inputs(enable=False)
    result = []
    for _ in range(17):
        simulator.tick()
        output_bits = [simulator.output(f"O[{index}]") for index in range(8)]
        known_value = sum(1 << index for index, value in enumerate(output_bits) if value is True)
        unknown_mask = sum(1 << index for index, value in enumerate(output_bits) if value is None)
        result.append({"known_value": known_value, "unknown_mask": unknown_mask})
    return result


def hex_bytes(trace: list[dict[str, int]]) -> str:
    if any(item["unknown_mask"] for item in trace):
        raise AssertionError("Cannot serialize a three-valued trace as exact bytes")
    return bytes(item["known_value"] for item in trace).hex()


def output_class_predicates(
    bits: list[Any], regions: list[list[int]]
) -> dict[str, Any]:
    def cell(row: int, column: int) -> Any:
        return bits[row * 11 + column]

    exact_counts = []
    for row in range(11):
        exact_counts.append(z3.PbEq([(cell(row, column), 1) for column in range(11)], 2))
    for column in range(11):
        exact_counts.append(z3.PbEq([(cell(row, column), 1) for row in range(11)], 2))
    for region in range(11):
        exact_counts.append(
            z3.PbEq(
                [
                    (cell(row, column), 1)
                    for row in range(11)
                    for column in range(11)
                    if regions[row][column] == region
                ],
                2,
            )
        )

    touching_pairs = []
    for row in range(11):
        for column in range(11):
            for row_delta, column_delta in ((0, 1), (1, -1), (1, 0), (1, 1)):
                other_row = row + row_delta
                other_column = column + column_delta
                if 0 <= other_row < 11 and 0 <= other_column < 11:
                    touching_pairs.append(
                        z3.And(cell(row, column), cell(other_row, other_column))
                    )

    counts_valid = z3.And(exact_counts)
    touching = z3.Or(touching_pairs)
    empty = z3.And([z3.Not(bit) for bit in bits])
    full = z3.And(bits)
    winner = z3.And(counts_valid, z3.Not(touching))
    counts_valid_touching = z3.And(counts_valid, touching)
    other = z3.Not(z3.Or(empty, full, winner, counts_valid_touching))
    return {
        "empty": empty,
        "full": full,
        "winner": winner,
        "counts_valid_touching": counts_valid_touching,
        "other": other,
    }


def prove_output_partition(
    netlist: dict[str, Any], regions: list[list[int]]
) -> dict[str, Any]:
    """Prove every output byte for all boards and both static float ties."""
    started = perf_counter()
    tied_netlist = json.loads(json.dumps(netlist))
    tied_netlist["ports"]["floating_probe"] = "n0653"
    output_nets = {tied_netlist["ports"][f"O[{bit}]"] for bit in range(8)}
    symbolic = SymbolicSimulator(tied_netlist, output_nets)
    state = symbolic.initial_state()
    floating_probe = z3.Bool("egg_floating_probe")
    inputs = {
        "clk": z3.BoolVal(True),
        "rst_n": z3.BoolVal(False),
        "enable": z3.BoolVal(False),
        "I": z3.BoolVal(False),
        "floating_probe": floating_probe,
    }
    for _ in range(3):
        state, _ = symbolic.tick(state, inputs)
    inputs["rst_n"] = z3.BoolVal(True)
    state, _ = symbolic.tick(state, inputs)

    bits = [z3.Bool(f"egg_input_{index:03d}") for index in range(121)]
    inputs["enable"] = z3.BoolVal(True)
    for bit in bits:
        inputs["I"] = bit
        state, _ = symbolic.tick(state, inputs)

    inputs["enable"] = z3.BoolVal(False)
    inputs["I"] = z3.BoolVal(False)
    output_trace: list[list[Any]] = []
    for _ in range(17):
        state, _ = symbolic.tick(state, inputs)
        values = symbolic.evaluate(state, inputs)
        output_trace.append([values[netlist["ports"][f"O[{bit}]"]] for bit in range(8)])

    predicates = output_class_predicates(bits, regions)
    messages: dict[str, bytes] = {
        "empty": b"EMPTY SKY".ljust(17, b"\0"),
        "full": b"BIG BANG".ljust(17, b"\0"),
        "winner": b"(* TWO STARS *)".ljust(17, b"\0"),
        "other": b"TRY AGAIN".ljust(17, b"\0"),
    }
    touching_low = b'TWO"NOT TOUCH'.ljust(17, b"\0")
    touching_high = b"TWO NOT TOUCJ\x02\x10".ljust(17, b"\0")
    differences = []
    for cycle, output_bits in enumerate(output_trace):
        for bit_index, actual in enumerate(output_bits):
            class_bits = {}
            for name in predicates:
                if name == "counts_valid_touching":
                    class_bits[name] = z3.If(
                        floating_probe,
                        z3.BoolVal(bool(touching_high[cycle] & (1 << bit_index))),
                        z3.BoolVal(bool(touching_low[cycle] & (1 << bit_index))),
                    )
                else:
                    class_bits[name] = z3.BoolVal(
                        bool(messages[name][cycle] & (1 << bit_index))
                    )
            expected = z3.Or(
                [
                    z3.And(predicate, class_bits[name])
                    for name, predicate in predicates.items()
                ]
            )
            differences.append(z3.Xor(actual, expected))

    solver = z3.Solver()
    solver.add(z3.Or(differences))
    status = solver.check()
    if status != z3.unsat:
        raise AssertionError(f"Five-way output partition has a counterexample ({status})")
    return {
        "counterexample_status": str(status),
        "seconds": perf_counter() - started,
        "boards_covered": "all 2^121 serial boards",
        "cycles_checked": len(output_trace),
        "output_bits_checked_per_cycle": 8,
        "floating_net_values_proved": [0, 1],
        "floating_net_convention": "n0653 is exposed as a Boolean symbolic input, so the query covers both static ties",
        "meaning": "UNSAT proves the complete five-way byte partition through seventeen output clocks",
        "symbolic_cone": symbolic.cone_stats,
    }


def board_rule_profile(
    bit_string: str,
    region_map: list[list[int]],
) -> tuple[list[list[bool]], list[int], list[int], list[int], list[list[list[int]]]]:
    board = [
        [
            bit_string[row * BOARD_SIZE + column] == "1"
            for column in range(BOARD_SIZE)
        ]
        for row in range(BOARD_SIZE)
    ]
    row_counts = [sum(row) for row in board]
    column_counts = [
        sum(board[row][column] for row in range(BOARD_SIZE))
        for column in range(BOARD_SIZE)
    ]
    region_counts = [
        sum(
            board[row][column]
            for row in range(BOARD_SIZE)
            for column in range(BOARD_SIZE)
            if region_map[row][column] == region
        )
        for region in range(BOARD_SIZE)
    ]
    touches: list[list[list[int]]] = []
    for row in range(BOARD_SIZE):
        for column in range(BOARD_SIZE):
            if not board[row][column]:
                continue
            for other_row in range(max(0, row - 1), min(BOARD_SIZE, row + 2)):
                for other_column in range(
                    max(0, column - 1), min(BOARD_SIZE, column + 2)
                ):
                    if (
                        (other_row, other_column) > (row, column)
                        and board[other_row][other_column]
                    ):
                        touches.append([[row, column], [other_row, other_column]])

    return board, row_counts, column_counts, region_counts, touches


def output_report(
    netlist: dict[str, Any],
    regions: dict[str, Any],
    solution: dict[str, Any],
    physical: dict[str, Any],
) -> dict[str, Any]:
    region_map = regions["regions"]
    board, row_counts, column_counts, region_counts, touches = board_rule_profile(
        TOUCHING_BOARD_WITNESS,
        region_map,
    )
    if (
        row_counts != [2] * BOARD_SIZE
        or column_counts != [2] * BOARD_SIZE
        or region_counts != [2] * BOARD_SIZE
        or not touches
    ):
        raise AssertionError("Fifth-branch witness no longer has the expected rule profile")

    low = replay(netlist, TOUCHING_BOARD_WITNESS, False)
    high = replay(netlist, TOUCHING_BOARD_WITNESS, True)
    unknown = replay(netlist, TOUCHING_BOARD_WITNESS, None)
    low_hex, high_hex = hex_bytes(low), hex_bytes(high)
    expected_low = b'TWO"NOT TOUCH\0\0\0\0'.hex()
    expected_high = b"TWO NOT TOUCJ\x02\x10\0\0".hex()
    if low_hex != expected_low or high_hex != expected_high:
        raise AssertionError("Fifth-branch tied output changed")

    models, instances, ports = netlist["models"], netlist["instances"], netlist["ports"]
    consumers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in instances:
        for pin, net in item["pins"].items():
            if models[item["cell"]]["pins"][pin] == "input":
                consumers[net].append(item)
    pending, seen, reached = deque(["n0653"]), {"n0653"}, set()
    while pending:
        net = pending.popleft()
        reached.update(name for name, value in ports.items() if value == net)
        for item in consumers[net]:
            if models[item["cell"]]["sequential"]:
                continue
            for pin, output_net in item["pins"].items():
                if models[item["cell"]]["pins"][pin] == "output" and output_net not in seen:
                    seen.add(output_net)
                    pending.append(output_net)
    if reached != {"O[1]", "O[4]"}:
        raise AssertionError(f"Unexpected floating-net output reachability: {reached}")

    classes = [
        {"id": "empty", "condition": "all 121 bits are zero", "output_ascii": "EMPTY SKY", "tie0_and_tie1": "454d50545920534b59"},
        {"id": "full", "condition": "all 121 bits are one", "output_ascii": "BIG BANG", "tie0_and_tie1": "4249472042414e47"},
        {"id": "winner", "condition": "all row/column/region counts are two and no stars touch", "output_ascii": solution["output_text"], "tie0_and_tie1": solution["output_hex"]},
        {"id": "counts_valid_touching", "condition": "all row/column/region counts are two, but at least one pair touches", "tie0_17_bytes_hex": low_hex, "tie1_17_bytes_hex": high_hex, "three_valued": unknown},
        {"id": "other", "condition": "none of the four conditions above", "output_ascii": "TRY AGAIN", "tie0_and_tie1": "54525920414741494e"},
    ]
    partition_proof = prove_output_partition(netlist, region_map)
    return {
        "five_way_partition": classes,
        "proof_scope": {
            "tie0_and_tie1": partition_proof,
            "floating_X": (
                "three-valued concrete replay; unknown_mask records X under "
                "pessimistic three-valued propagation and does not prove that "
                "a bit is free across Boolean tie assignments"
            ),
        },
        "dangling_net": {
            "net": "n0653", "logical_endpoints": ["U0516.A1", "U0521.A1"],
            "reachable_outputs": sorted(reached), "physical_audit": physical,
        },
        "fifth_branch_witness": {
            "bits": TOUCHING_BOARD_WITNESS,
            "rows": ["".join("#" if value else "." for value in row) for row in board],
            "row_counts": row_counts, "column_counts": column_counts,
            "region_counts": region_counts, "touching_pairs": touches,
            "tie0_17_bytes_hex": low_hex, "tie1_17_bytes_hex": high_hex,
            "three_valued_17_cycles": unknown,
            "interpretation": "The fixed characters read TWO?NOT TOUC?; replacing either unknown by a preferred letter is not justified by the extracted circuit.",
        },
        "confidence": "four ordinary messages are stable; fifth branch is exact only after declaring n0653=0 or 1",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vcd", type=Path, default=ROOT / "example_inputs.vcd")
    parser.add_argument("--gds", type=Path, default=ROOT / "puzzle.gds")
    parser.add_argument("--netlist", type=Path, default=ROOT / "build/puzzle_netlist.json")
    parser.add_argument("--regions", type=Path, default=ROOT / "build/regions.json")
    parser.add_argument("--solution", type=Path, default=ROOT / "build/solution.json")
    parser.add_argument("--output", type=Path, default=ROOT / "build/easter_eggs.json")
    args = parser.parse_args()
    netlist = json.loads(args.netlist.read_text())
    regions = json.loads(args.regions.read_text())
    solution = json.loads(args.solution.read_text())
    morse, physical = decode_morse(args.gds, netlist)
    payload = {
        "schema_version": 1,
        "vcd": decode_vcd(args.vcd),
        "morse": morse,
        "output_classes": output_report(netlist, regions, solution, physical),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Decoded VCD: {payload['vcd']['phrase']}")
    print(f"Decoded GDS Morse: {payload['morse']['phrase']}")
    print("Characterized five output classes (including the n0653 caveat)")


if __name__ == "__main__":
    main()
