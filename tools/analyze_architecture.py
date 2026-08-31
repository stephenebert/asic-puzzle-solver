#!/usr/bin/env python3
"""Map every puzzle flip-flop and functional combinational cell by role.

The semantic role map is checked against the exact flip-flop set, recovered
counter markers, one-cycle dependency cones, and complete combinational-cell
coverage.  The output includes coordinates and compact state and gate counts
so it can be inspected without reopening the full netlist.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent

COLUMN_PAIRS = [
    ("U0186", "U0201"), ("U0110", "U0182"), ("U0181", "U0109"),
    ("U0118", "U0180"), ("U0171", "U0099"), ("U0098", "U0170"),
    ("U0100", "U0172"), ("U0113", "U0145"), ("U0116", "U0134"),
    ("U0114", "U0133"), ("U0115", "U0135"),
]
REGION_PAIRS = [
    ("U0616", "U0588"), ("U0223", "U0225"), ("U0613", "U0591"),
    ("U0605", "U0587"), ("U0207", "U0189"), ("U0651", "U0586"),
    ("U0642", "U0638"), ("U0593", "U0626"), ("U0218", "U0190"),
    ("U0224", "U0592"), ("U0594", "U0625"),
]

ROLE_MAP = {
    "serial_position": {
        "U0347": "column phase bit 0", "U0354": "column phase bit 1",
        "U0344": "column phase bit 2", "U0346": "column phase bit 3",
        "U0462": "row phase bit 0", "U0466": "row phase bit 1",
        "U0459": "row phase bit 2", "U0458": "row phase bit 3",
        "U0342": "121-bit terminal flag",
    },
    "population_count": {
        "U0444": "total-star count bit 0", "U0442": "total-star count bit 1",
        "U0440": "total-star count bit 2", "U0438": "total-star count bit 3",
        "U0451": "total-star count bit 4", "U0450": "total-star count bit 5",
        "U0452": "total-star count bit 6", "U0445": "total-star count bit 7",
    },
    "row_rule": {
        "U0411": "saturating row-star count bit 0",
        "U0407": "saturating row-star count bit 1",
        "U0410": "sticky row-count violation",
    },
    "no_touch": {
        "U0361": "current/last serial bit",
        "U0390": "serial history delay 1", "U0370": "serial history delay 2",
        "U0388": "serial history delay 3", "U0362": "serial history delay 4",
        "U0384": "serial history delay 5", "U0359": "serial history delay 6",
        "U0395": "serial history delay 7", "U0385": "serial history delay 8",
        "U0386": "serial history delay 9", "U0382": "serial history delay 10",
        "U0380": "serial history delay 11", "U0381": "sticky touching violation",
    },
    "output_control": {
        "U0026": "output phase active", "U0027": "auxiliary verdict/control latch",
        "U0028": "success latch",
        "U0035": "character index bit 0", "U0036": "character index bit 1",
        "U0034": "character index bit 2", "U0037": "character index bit 3",
        "U0257": "output datapath bit 0", "U0244": "output datapath bit 1",
        "U0255": "output datapath bit 2", "U0245": "output datapath bit 3",
        "U0247": "output datapath bit 4", "U0256": "output datapath bit 5",
        "U0246": "output datapath bit 6", "U0258": "output datapath bit 7",
    },
}

DESCRIPTIONS = {
    "serial_position": "Tracks the 11-column phase, 11-row phase, and end of the 121-bit load.",
    "population_count": "An eight-bit binary population counter; a 121-one trace visits decoded values 0 through 121.",
    "row_rule": "Counts stars within a row, saturates at three, and remembers any row whose count was not two.",
    "no_touch": "A twelve-bit sliding serial history plus a sticky horizontal/vertical/diagonal contact error.",
    "column_counters": "Eleven independent two-bit star counters, one for each column.",
    "region_counters": "Eleven independent two-bit star counters, one for each recovered irregular region.",
    "output_control": "Latches success, steps a four-bit character index, and generates the eight output bits.",
}


def flatten(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {name: f"two-bit counter {index}, bit {bit}" for index, pair in enumerate(pairs) for bit, name in enumerate(pair)}


def build(netlist: dict[str, Any], regions: dict[str, Any]) -> dict[str, Any]:
    models = netlist["models"]
    instances = netlist["instances"]
    by_name = {item["name"]: item for item in instances}
    sequential = {item["name"] for item in instances if models[item["cell"]]["sequential"]}
    combinational = {item["name"] for item in instances if not models[item["cell"]]["sequential"]}

    roles = {key: dict(value) for key, value in ROLE_MAP.items()}
    roles["column_counters"] = flatten(COLUMN_PAIRS)
    roles["region_counters"] = flatten(REGION_PAIRS)
    assigned = {name for mapping in roles.values() for name in mapping}
    if assigned != sequential or sum(len(mapping) for mapping in roles.values()) != len(assigned):
        raise AssertionError(f"FF classification mismatch: missing={sorted(sequential-assigned)}, extra={sorted(assigned-sequential)}")

    driver: dict[str, dict[str, Any]] = {}
    for item in instances:
        for pin, net in item["pins"].items():
            if models[item["cell"]]["pins"][pin] == "output":
                driver[net] = item

    def cone(targets: list[str]) -> set[str]:
        pending, seen, cells = list(targets), set(), set()
        while pending:
            net = pending.pop()
            if net in seen:
                continue
            seen.add(net)
            item = driver.get(net)
            if item is None or item["name"] in sequential:
                continue
            cells.add(item["name"])
            pending.extend(net for pin, net in item["pins"].items() if models[item["cell"]]["pins"][pin] == "input")
        return cells

    cones = {}
    for group, mapping in roles.items():
        targets = [by_name[name]["pins"]["D"] for name in mapping]
        if group == "output_control":
            targets += [netlist["ports"][f"O[{bit}]"] for bit in range(8)]
        cones[group] = cone(targets)
    memberships: dict[str, list[str]] = defaultdict(list)
    for group, cells in cones.items():
        for cell in cells:
            memberships[cell].append(group)

    clock_buffers = {item["name"] for item in instances if models[item["cell"]]["family"] == "clkbuf"}
    if set(memberships) | clock_buffers != combinational or set(memberships) & clock_buffers:
        raise AssertionError("Logical cones plus clock tree do not cover combinational cells exactly")

    groups = []
    for group, mapping in roles.items():
        ff_items = [
            {"name": name, "x": by_name[name]["x"], "y": by_name[name]["y"], "role": role}
            for name, role in mapping.items()
        ]
        groups.append({
            "id": group,
            "label": group.replace("_", " ").title(),
            "ff_count": len(mapping),
            "ff_instances": ff_items,
            "combinational_cone_count": len(cones[group]),
            "exclusive_combinational_count": sum(memberships[cell] == [group] for cell in cones[group]),
            "description": DESCRIPTIONS[group],
            "evidence": "curated semantic role; exact FF coverage; one-cycle D/output cone",
        })

    column_markers = regions["column_counter_markers"]
    region_markers = regions["region_counter_markers"]
    if [pair[0] for pair in COLUMN_PAIRS] != column_markers:
        raise AssertionError("Column counter pairs differ from recovered columns")
    if any(region_markers.get(pair[0]) != index for index, pair in enumerate(REGION_PAIRS)):
        raise AssertionError("Region counter pair order differs from recovered regions")
    counter_banks = {
        "columns": [
            {"index": index, "instances": list(pair), "marker": column_markers[index]}
            for index, pair in enumerate(COLUMN_PAIRS)
        ],
        "regions": [
            {"index": index, "instances": list(pair), "marker": pair[0], "size": regions["region_sizes"][index]}
            for index, pair in enumerate(REGION_PAIRS)
        ],
    }
    if {entry["marker"] for entry in counter_banks["regions"]} != set(region_markers):
        raise AssertionError("Region counter markers differ from recovered regions")

    dependencies = set()
    group_for = {name: group for group, mapping in roles.items() for name in mapping}
    for target_group, mapping in roles.items():
        for name in mapping:
            pending, seen = [by_name[name]["pins"]["D"]], set()
            while pending:
                net = pending.pop()
                if net in seen:
                    continue
                seen.add(net)
                item = driver.get(net)
                if item is None:
                    continue
                if item["name"] in sequential:
                    source_group = group_for[item["name"]]
                    if source_group != target_group:
                        dependencies.add((source_group, target_group))
                    continue
                pending.extend(net for pin, net in item["pins"].items() if models[item["cell"]]["pins"][pin] == "input")

    totals = {
        "instances": len(instances), "flip_flops": len(sequential),
        "combinational": len(combinational), "clock_buffers": len(clock_buffers),
        "functional_combinational": len(memberships),
    }
    return {
        "schema_version": 1,
        "source_netlist": netlist.get("source"),
        "totals": totals,
        "groups": groups,
        "counter_banks": counter_banks,
        "combinational_membership": {
            "union_count": len(memberships),
            "exclusive_count": sum(len(value) == 1 for value in memberships.values()),
            "shared_count": sum(len(value) > 1 for value in memberships.values()),
            "membership_size_histogram": dict(sorted(Counter(map(len, memberships.values())).items())),
        },
        "dataflow_edges": [
            {"source": source, "target": target, "kind": "state dependency"}
            for source, target in sorted(dependencies)
        ],
        "visual_data": {
            "state_budget": [{"group": item["label"], "count": item["ff_count"]} for item in groups],
            "gate_budget": [{"group": item["label"], "exclusive": item["exclusive_combinational_count"], "cone": item["combinational_cone_count"]} for item in groups],
        },
        "classification_method": (
            "manually curated semantic roles with automated checks for exact FF coverage, "
            "counter-marker identity/order, dependency cones, and combinational-cell coverage"
        ),
        "confidence": "structurally checked classification under the recovered gate-level netlist",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netlist", type=Path, default=ROOT / "build/puzzle_netlist.json")
    parser.add_argument("--regions", type=Path, default=ROOT / "build/regions.json")
    parser.add_argument("--output", type=Path, default=ROOT / "build/architecture.json")
    args = parser.parse_args()
    payload = build(json.loads(args.netlist.read_text()), json.loads(args.regions.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Mapped {payload['totals']['flip_flops']} FFs and {payload['totals']['functional_combinational']} functional combinational cells")


if __name__ == "__main__":
    main()
