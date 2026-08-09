#!/usr/bin/env python3
"""Recover a standard-cell netlist from the routed metal in a GDS file.

The script is tailored to the layer mapping used by the Jane Street puzzle:
li1 through met5 are GDS layers 67 through 72, and a via cut between layer N
and N + 1 is stored as layer N, datatype 44.  It does not inspect transistor
geometry; the standard-cell hierarchy and pin labels already provide cell
recognition and pin access locations.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
from typing import Any

import gdstk
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree


CONDUCTOR_LAYERS = tuple(range(67, 73))
DRAWING_DATATYPE = 20
VIA_DATATYPE = 44
PIN_TEXTTYPE = 5
POWER_PINS = {"VGND", "VPWR", "VNB", "VPB"}
PHYSICAL_CELL_MARKERS = ("__tap", "__decap", "__diode")
LIBRARY_PREFIX = "sky130_fd_sc_hd__"


class DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[tuple[int, int], tuple[int, int]] = {}

    def find(self, item: tuple[int, int]) -> tuple[int, int]:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: tuple[int, int], right: tuple[int, int]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def unioned_conductors(top: gdstk.Cell) -> dict[int, tuple[list[Any], STRtree]]:
    layers: dict[int, tuple[list[Any], STRtree]] = {}
    for layer in CONDUCTOR_LAYERS:
        polygons = [
            Polygon(polygon.points)
            for polygon in top.get_polygons(
                layer=layer, datatype=DRAWING_DATATYPE
            )
            if len(polygon.points) >= 3
        ]
        merged = unary_union(polygons)
        components = (
            list(merged.geoms) if merged.geom_type == "MultiPolygon" else [merged]
        )
        components = [component for component in components if not component.is_empty]
        layers[layer] = (components, STRtree(components))
    return layers


def locate(
    layers: dict[int, tuple[list[Any], STRtree]], layer: int, geometry: Any
) -> list[int]:
    matches = [
        int(index)
        for index in layers[layer][1].query(geometry, predicate="intersects")
    ]
    if not matches:
        raise ValueError(
            f"No conductor on layer {layer} intersects geometry {geometry.bounds}"
        )
    return matches


def connect_vias(
    top: gdstk.Cell,
    layers: dict[int, tuple[list[Any], STRtree]],
) -> DisjointSet:
    connected = DisjointSet()
    for layer, (components, _) in layers.items():
        for index in range(len(components)):
            connected.find((layer, index))

    for lower_layer in CONDUCTOR_LAYERS[:-1]:
        for cut in top.get_polygons(layer=lower_layer, datatype=VIA_DATATYPE):
            cut_geometry = Polygon(cut.points)
            nodes = [
                (lower_layer, index)
                for index in locate(layers, lower_layer, cut_geometry)
            ] + [
                (lower_layer + 1, index)
                for index in locate(layers, lower_layer + 1, cut_geometry)
            ]
            for node in nodes[1:]:
                connected.union(nodes[0], node)
    return connected


def sky130_model(cell_name: str, vendor_root: Path) -> dict[str, Any]:
    short_name = cell_name.removeprefix(LIBRARY_PREFIX)
    family = re.sub(r"_\d+$", "", short_name)
    liberty_path = (
        vendor_root
        / "cells"
        / family
        / f"{LIBRARY_PREFIX}{short_name}__tt_025C_1v80.lib.json"
    )
    data = json.loads(liberty_path.read_text())
    pins = {
        key.removeprefix("pin,"): value["direction"]
        for key, value in data.items()
        if key.startswith("pin,")
    }
    outputs = {
        key.removeprefix("pin,"): value.get("function")
        for key, value in data.items()
        if key.startswith("pin,") and value["direction"] == "output"
    }
    sequential = {
        key: value
        for key, value in data.items()
        if key.startswith(("ff,", "latch,"))
    }
    return {
        "family": family,
        "pins": pins,
        "outputs": outputs,
        "sequential": sequential,
        "liberty": str(liberty_path),
    }


def extract(gds_path: Path, vendor_root: Path) -> dict[str, Any]:
    library = gdstk.read_gds(gds_path)
    top_cells = library.top_level()
    if len(top_cells) != 1:
        raise ValueError(f"Expected exactly one top cell, got {len(top_cells)}")
    top = top_cells[0]

    layers = unioned_conductors(top)
    connected = connect_vias(top, layers)
    raw_instances: list[dict[str, Any]] = []
    used_models: dict[str, dict[str, Any]] = {}

    for reference_index, reference in enumerate(top.references):
        cell_name = reference.cell_name
        if not cell_name.startswith(LIBRARY_PREFIX):
            continue
        if any(marker in cell_name for marker in PHYSICAL_CELL_MARKERS):
            continue

        if cell_name not in used_models:
            used_models[cell_name] = sky130_model(cell_name, vendor_root)
        model = used_models[cell_name]
        pin_roots: dict[str, set[tuple[int, int]]] = defaultdict(set)
        for label in reference.get_labels(layer=67, texttype=PIN_TEXTTYPE):
            if label.text in POWER_PINS:
                continue
            if label.text not in model["pins"]:
                raise ValueError(f"Unknown pin label {cell_name}.{label.text}")
            for component in locate(layers, 67, Point(label.origin)):
                pin_roots[label.text].add(connected.find((67, component)))

        if not pin_roots:
            continue
        for pin_name, roots in pin_roots.items():
            if len(roots) != 1:
                raise ValueError(
                    f"Pin {cell_name}.{pin_name} touches multiple nets: {roots}"
                )

        raw_instances.append(
            {
                "name": f"U{len(raw_instances):04d}",
                "reference_index": reference_index,
                "cell": cell_name,
                "x": reference.origin[0],
                "y": reference.origin[1],
                "rotation": reference.rotation or 0.0,
                "x_reflection": reference.x_reflection,
                "pin_roots": {
                    pin: next(iter(roots)) for pin, roots in pin_roots.items()
                },
            }
        )

    port_roots: dict[str, tuple[int, int]] = {}
    for label in top.labels:
        if label.text in POWER_PINS or label.layer not in layers:
            continue
        matches = {
            connected.find((label.layer, component))
            for component in locate(layers, label.layer, Point(label.origin))
        }
        if len(matches) != 1:
            raise ValueError(f"Port {label.text} touches multiple nets: {matches}")
        port_roots[label.text] = next(iter(matches))

    signal_roots = set(port_roots.values())
    for instance in raw_instances:
        signal_roots.update(instance["pin_roots"].values())
    root_to_net = {
        root: f"n{index:04d}" for index, root in enumerate(sorted(signal_roots))
    }

    instances = []
    for instance in raw_instances:
        instance = dict(instance)
        instance["pins"] = {
            pin: root_to_net[root]
            for pin, root in instance.pop("pin_roots").items()
        }
        instances.append(instance)
    ports = {name: root_to_net[root] for name, root in port_roots.items()}

    connections: dict[str, list[dict[str, str]]] = defaultdict(list)
    for instance in instances:
        model = used_models[instance["cell"]]
        for pin, net in instance["pins"].items():
            connections[net].append(
                {
                    "instance": instance["name"],
                    "pin": pin,
                    "direction": model["pins"][pin],
                }
            )
    for port, net in ports.items():
        connections[net].append({"port": port})

    dangling_inputs = []
    multiple_drivers = []
    for net, endpoints in connections.items():
        drivers = [e for e in endpoints if e.get("direction") == "output"]
        has_port = any("port" in e for e in endpoints)
        loads = [e for e in endpoints if e.get("direction") == "input"]
        if loads and not drivers and not has_port:
            dangling_inputs.append(net)
        if len(drivers) > 1:
            multiple_drivers.append(net)

    return {
        "source": str(gds_path),
        "top": top.name,
        "ports": ports,
        "instances": instances,
        "models": used_models,
        "nets": dict(sorted(connections.items())),
        "stats": {
            "instances": len(instances),
            "nets": len(connections),
            "dangling_inputs": dangling_inputs,
            "multiple_drivers": multiple_drivers,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gds", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--vendor-root",
        type=Path,
        default=Path("vendor/sky130_fd_sc_hd"),
    )
    args = parser.parse_args()

    result = extract(args.gds, args.vendor_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result["stats"], indent=2))


if __name__ == "__main__":
    main()
