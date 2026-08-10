#!/usr/bin/env python3
"""Independently cross-check the puzzle netlist with KLayout.

This extractor deliberately does not import the primary gdstk/Shapely
extractor.  KLayout reads the GDS, merges conductor geometry, performs exact
polygon intersection and point-in-polygon tests, and supplies all hierarchy
and text transforms.

The extraction is at standard-cell level: cell identity and pin access points
come from the GDS hierarchy and pin labels, while pin directions come from the
vendored SKY130 Liberty JSON.  Power pins and physical-only tap/decap/diode
cells are excluded, matching the functional netlist's scope.

With no arguments, the script reads ``puzzle.gds``, writes the independent
netlist to ``build/puzzle_netlist_klayout.json``, compares it with
``build/puzzle_netlist.json``, and writes a compact comparison report to
``build/klayout_crosscheck.json``.  Net names are intentionally ignored.  A
connectivity mismatch is reported on stdout and produces exit status 1.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Iterable, Iterator

KLAYOUT_IMPORT_ERROR: ImportError | None = None
try:
    import klayout
    import klayout.db as kdb
except ImportError as error:  # pragma: no cover - depends on local installation
    klayout = None
    kdb = None
    KLAYOUT_IMPORT_ERROR = error


CONDUCTOR_LAYERS = tuple(range(67, 73))
DRAWING_DATATYPE = 20
VIA_DATATYPE = 44
PIN_TEXTTYPE = 5
POWER_PINS = {"VGND", "VPWR", "VNB", "VPB"}
PHYSICAL_CELL_MARKERS = ("__tap", "__decap", "__diode")
LIBRARY_PREFIX = "sky130_fd_sc_hd__"
DEFAULT_GRID_SIZE_DBU = 5_000

Node = tuple[int, int]
InstanceKey = tuple[str, int, int, int, bool]
Endpoint = tuple[Any, ...]
NetSignature = tuple[Endpoint, ...]


class DisjointSet:
    """Small union-find over (GDS layer, merged-component index) nodes."""

    def __init__(self) -> None:
        self.parent: dict[Node, Node] = {}

    def find(self, item: Node) -> Node:
        self.parent.setdefault(item, item)
        if self.parent[item] != item:
            self.parent[item] = self.find(self.parent[item])
        return self.parent[item]

    def union(self, left: Node, right: Node) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


@dataclass
class PolygonIndex:
    """A simple bbox grid whose exact predicates are evaluated by KLayout."""

    polygons: list[Any]
    grid_size: int = DEFAULT_GRID_SIZE_DBU

    def __post_init__(self) -> None:
        self._buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for index, polygon in enumerate(self.polygons):
            for bucket in self._buckets_for_box(polygon.bbox()):
                self._buckets[bucket].append(index)

    def _buckets_for_box(self, box: Any) -> Iterator[tuple[int, int]]:
        left = box.left // self.grid_size
        right = box.right // self.grid_size
        bottom = box.bottom // self.grid_size
        top = box.top // self.grid_size
        for x_bucket in range(left, right + 1):
            for y_bucket in range(bottom, top + 1):
                yield (x_bucket, y_bucket)

    def candidates(self, box: Any) -> Iterable[int]:
        seen: set[int] = set()
        for bucket in self._buckets_for_box(box):
            for index in self._buckets.get(bucket, ()):
                if index not in seen:
                    seen.add(index)
                    yield index

    def locate_point(self, point: Any) -> list[int]:
        point_box = kdb.Box(point.x, point.y, point.x, point.y)
        return [
            index
            for index in self.candidates(point_box)
            if self.polygons[index].inside(point)
        ]

    def intersecting(self, polygon: Any) -> list[int]:
        query_region = kdb.Region(polygon)
        matches = []
        for index in self.candidates(polygon.bbox()):
            candidate = self.polygons[index]
            if not candidate.bbox().overlaps(polygon.bbox()):
                continue
            if not (kdb.Region(candidate) & query_region).is_empty():
                matches.append(index)
        return matches


def require_layer(layout: Any, layer: int, datatype: int) -> int:
    layer_index = layout.find_layer(kdb.LayerInfo(layer, datatype))
    if layer_index is None:
        raise ValueError(f"Missing GDS layer {layer}/{datatype}")
    return int(layer_index)


def merged_conductors(
    layout: Any, top: Any
) -> tuple[dict[int, PolygonIndex], dict[str, int]]:
    indexes: dict[int, PolygonIndex] = {}
    component_counts: dict[str, int] = {}
    for layer in CONDUCTOR_LAYERS:
        layer_index = require_layer(layout, layer, DRAWING_DATATYPE)
        region = kdb.Region(top.begin_shapes_rec(layer_index))
        region.merge()
        polygons = list(region.each())
        indexes[layer] = PolygonIndex(polygons)
        component_counts[str(layer)] = len(polygons)
    return indexes, component_counts


def locate_point(index: PolygonIndex, point: Any, description: str) -> list[int]:
    matches = index.locate_point(point)
    if not matches:
        raise ValueError(
            f"No conductor contains {description} at ({point.x}, {point.y}) DBU"
        )
    return matches


def connect_vias(
    layout: Any,
    top: Any,
    layers: dict[int, PolygonIndex],
) -> tuple[DisjointSet, dict[str, int]]:
    connected = DisjointSet()
    for layer, index in layers.items():
        for component in range(len(index.polygons)):
            connected.find((layer, component))

    via_counts: dict[str, int] = {}
    for lower_layer in CONDUCTOR_LAYERS[:-1]:
        via_layer_index = require_layer(layout, lower_layer, VIA_DATATYPE)
        cuts = list(kdb.Region(top.begin_shapes_rec(via_layer_index)).each())
        via_counts[str(lower_layer)] = len(cuts)
        for cut_number, cut in enumerate(cuts):
            nodes = [
                (lower_layer, index)
                for index in layers[lower_layer].intersecting(cut)
            ] + [
                (lower_layer + 1, index)
                for index in layers[lower_layer + 1].intersecting(cut)
            ]
            if not any(node[0] == lower_layer for node in nodes):
                raise ValueError(
                    f"Via {lower_layer}/{VIA_DATATYPE} #{cut_number} misses "
                    f"lower conductor {lower_layer}/{DRAWING_DATATYPE}"
                )
            if not any(node[0] == lower_layer + 1 for node in nodes):
                raise ValueError(
                    f"Via {lower_layer}/{VIA_DATATYPE} #{cut_number} misses "
                    f"upper conductor {lower_layer + 1}/{DRAWING_DATATYPE}"
                )
            for node in nodes[1:]:
                connected.union(nodes[0], node)
    return connected, via_counts


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


def is_functional_cell(cell_name: str) -> bool:
    return cell_name.startswith(LIBRARY_PREFIX) and not any(
        marker in cell_name for marker in PHYSICAL_CELL_MARKERS
    )


def text_origin(text: Any) -> Any:
    displacement = text.trans.disp
    return kdb.Point(displacement.x, displacement.y)


def extract_instances(
    layout: Any,
    top: Any,
    layers: dict[int, PolygonIndex],
    connected: DisjointSet,
    vendor_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    pin_layer_index = require_layer(layout, 67, PIN_TEXTTYPE)
    raw_instances: list[dict[str, Any]] = []
    used_models: dict[str, dict[str, Any]] = {}

    for reference_index, instance in enumerate(top.each_inst()):
        cell = layout.cell(instance.cell_index)
        cell_name = cell.name
        if not is_functional_cell(cell_name):
            continue
        if instance.is_regular_array():
            raise ValueError(f"Functional instance array is unsupported: {cell_name}")

        if cell_name not in used_models:
            used_models[cell_name] = sky130_model(cell_name, vendor_root)
        model = used_models[cell_name]
        pin_roots: dict[str, set[Node]] = defaultdict(set)

        for shape in cell.shapes(pin_layer_index).each():
            if not shape.is_text():
                continue
            label = shape.text
            if label.string in POWER_PINS:
                continue
            if label.string not in model["pins"]:
                raise ValueError(f"Unknown pin label {cell_name}.{label.string}")
            origin = instance.cplx_trans * text_origin(label)
            for component in locate_point(
                layers[67], origin, f"pin {cell_name}.{label.string}"
            ):
                pin_roots[label.string].add(connected.find((67, component)))

        if not pin_roots:
            continue
        for pin_name, roots in pin_roots.items():
            if len(roots) != 1:
                raise ValueError(
                    f"Pin {cell_name}.{pin_name} touches multiple nets: {roots}"
                )

        transform = instance.trans
        raw_instances.append(
            {
                "name": f"K{len(raw_instances):04d}",
                "reference_index": reference_index,
                "cell": cell_name,
                "x_dbu": int(transform.disp.x),
                "y_dbu": int(transform.disp.y),
                "rotation_degrees": int(transform.angle) * 90,
                "x_reflection": bool(transform.is_mirror()),
                "pin_roots": {
                    pin: next(iter(roots)) for pin, roots in pin_roots.items()
                },
            }
        )
    return raw_instances, used_models


def extract_ports(
    layout: Any,
    top: Any,
    layers: dict[int, PolygonIndex],
    connected: DisjointSet,
) -> dict[str, Node]:
    port_roots: dict[str, Node] = {}
    for layer in CONDUCTOR_LAYERS:
        text_layer = layout.find_layer(kdb.LayerInfo(layer, PIN_TEXTTYPE))
        if text_layer is None:
            continue
        for shape in top.shapes(text_layer).each():
            if not shape.is_text():
                continue
            label = shape.text
            if label.string in POWER_PINS:
                continue
            origin = text_origin(label)
            matches = {
                connected.find((layer, component))
                for component in locate_point(
                    layers[layer], origin, f"port {label.string}"
                )
            }
            if len(matches) != 1:
                raise ValueError(
                    f"Port {label.string} touches multiple nets: {matches}"
                )
            if label.string in port_roots and port_roots[label.string] not in matches:
                raise ValueError(f"Port {label.string} appears on different nets")
            port_roots[label.string] = next(iter(matches))
    return port_roots


def finish_netlist(
    source: Path,
    top: Any,
    dbu: float,
    raw_instances: list[dict[str, Any]],
    models: dict[str, dict[str, Any]],
    port_roots: dict[str, Node],
    component_counts: dict[str, int],
    via_counts: dict[str, int],
    timings: dict[str, float],
) -> dict[str, Any]:
    signal_roots = set(port_roots.values())
    for instance in raw_instances:
        signal_roots.update(instance["pin_roots"].values())
    root_to_net = {
        root: f"k{index:04d}" for index, root in enumerate(sorted(signal_roots))
    }

    instances = []
    for raw_instance in raw_instances:
        instance = dict(raw_instance)
        instance["x"] = instance.pop("x_dbu") * dbu
        instance["y"] = instance.pop("y_dbu") * dbu
        instance["rotation"] = math.radians(instance.pop("rotation_degrees"))
        instance["pins"] = {
            pin: root_to_net[root]
            for pin, root in instance.pop("pin_roots").items()
        }
        instances.append(instance)
    ports = {name: root_to_net[root] for name, root in port_roots.items()}

    connections: dict[str, list[dict[str, str]]] = defaultdict(list)
    for instance in instances:
        model = models[instance["cell"]]
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
        "source": str(source),
        "extractor": f"KLayout {klayout.__version__}",
        "dbu": dbu,
        "top": top.name,
        "ports": ports,
        "instances": instances,
        "models": models,
        "nets": dict(sorted(connections.items())),
        "stats": {
            "instances": len(instances),
            "nets": len(connections),
            "dangling_inputs": sorted(dangling_inputs),
            "multiple_drivers": sorted(multiple_drivers),
            "conductor_components": component_counts,
            "via_cuts": via_counts,
            "timings_seconds": timings,
        },
    }


def extract(gds_path: Path, vendor_root: Path) -> dict[str, Any]:
    started = time.perf_counter()
    timings: dict[str, float] = {}

    layout = kdb.Layout()
    layout.read(str(gds_path))
    timings["read_gds"] = time.perf_counter() - started
    top_cells = list(layout.top_cells())
    if len(top_cells) != 1:
        raise ValueError(f"Expected exactly one top cell, got {len(top_cells)}")
    top = top_cells[0]

    phase = time.perf_counter()
    layers, component_counts = merged_conductors(layout, top)
    timings["merge_conductors"] = time.perf_counter() - phase

    phase = time.perf_counter()
    connected, via_counts = connect_vias(layout, top, layers)
    timings["connect_vias"] = time.perf_counter() - phase

    phase = time.perf_counter()
    raw_instances, models = extract_instances(
        layout, top, layers, connected, vendor_root
    )
    timings["locate_cell_pins"] = time.perf_counter() - phase

    phase = time.perf_counter()
    port_roots = extract_ports(layout, top, layers, connected)
    timings["locate_ports"] = time.perf_counter() - phase
    timings["total"] = time.perf_counter() - started

    return finish_netlist(
        gds_path,
        top,
        layout.dbu,
        raw_instances,
        models,
        port_roots,
        component_counts,
        via_counts,
        timings,
    )


def instance_key(instance: dict[str, Any], dbu: float) -> InstanceKey:
    return (
        instance["cell"],
        round(instance["x"] / dbu),
        round(instance["y"] / dbu),
        round(math.degrees(instance.get("rotation", 0.0))) % 360,
        bool(instance.get("x_reflection", False)),
    )


def index_instances(
    netlist: dict[str, Any], dbu: float
) -> tuple[dict[str, InstanceKey], list[InstanceKey]]:
    by_name: dict[str, InstanceKey] = {}
    duplicates: list[InstanceKey] = []
    seen: set[InstanceKey] = set()
    for instance in netlist["instances"]:
        key = instance_key(instance, dbu)
        by_name[instance["name"]] = key
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    return by_name, duplicates


def net_signatures(
    netlist: dict[str, Any], instances: dict[str, InstanceKey]
) -> Counter[NetSignature]:
    signatures: Counter[NetSignature] = Counter()
    for endpoints in netlist["nets"].values():
        signature: list[Endpoint] = []
        for endpoint in endpoints:
            if "port" in endpoint:
                signature.append(("port", endpoint["port"]))
            else:
                signature.append(
                    (
                        "pin",
                        *instances[endpoint["instance"]],
                        endpoint["pin"],
                    )
                )
        signatures[tuple(sorted(signature, key=repr))] += 1
    return signatures


def compact_signature(signature: NetSignature) -> list[str]:
    result = []
    for endpoint in signature:
        if endpoint[0] == "port":
            result.append(f"port:{endpoint[1]}")
        else:
            _, cell, x, y, rotation, mirror, pin = endpoint
            orientation = f"r{rotation}" + ("m" if mirror else "")
            result.append(f"{cell.removeprefix(LIBRARY_PREFIX)}@{x},{y}/{orientation}.{pin}")
    return result


def compare_netlists(
    actual: dict[str, Any], expected: dict[str, Any], dbu: float
) -> dict[str, Any]:
    actual_instances, actual_duplicates = index_instances(actual, dbu)
    expected_instances, expected_duplicates = index_instances(expected, dbu)
    actual_keys = set(actual_instances.values())
    expected_keys = set(expected_instances.values())

    actual_signatures = net_signatures(actual, actual_instances)
    expected_signatures = net_signatures(expected, expected_instances)
    missing_signatures = expected_signatures - actual_signatures
    extra_signatures = actual_signatures - expected_signatures

    actual_cell_types = Counter(
        instance["cell"] for instance in actual["instances"]
    )
    expected_cell_types = Counter(
        instance["cell"] for instance in expected["instances"]
    )
    actual_ports = set(actual["ports"])
    expected_ports = set(expected["ports"])

    matched = not any(
        (
            actual["top"] != expected["top"],
            actual_duplicates,
            expected_duplicates,
            expected_keys - actual_keys,
            actual_keys - expected_keys,
            expected_cell_types - actual_cell_types,
            actual_cell_types - expected_cell_types,
            expected_ports - actual_ports,
            actual_ports - expected_ports,
            missing_signatures,
            extra_signatures,
        )
    )
    return {
        "matched": matched,
        "comparison": "net names ignored; nets matched as endpoint partitions",
        "top": {
            "actual": actual["top"],
            "expected": expected["top"],
            "matched": actual["top"] == expected["top"],
        },
        "counts": {
            "actual_instances": len(actual["instances"]),
            "expected_instances": len(expected["instances"]),
            "actual_nets": len(actual["nets"]),
            "expected_nets": len(expected["nets"]),
            "actual_ports": len(actual["ports"]),
            "expected_ports": len(expected["ports"]),
            "actual_cell_types": len(actual_cell_types),
            "expected_cell_types": len(expected_cell_types),
            "matched_net_signatures": sum((actual_signatures & expected_signatures).values()),
            "missing_net_signatures": sum(missing_signatures.values()),
            "extra_net_signatures": sum(extra_signatures.values()),
        },
        "differences": {
            "duplicate_actual_instances": [repr(key) for key in actual_duplicates],
            "duplicate_expected_instances": [repr(key) for key in expected_duplicates],
            "missing_instances": [repr(key) for key in sorted(expected_keys - actual_keys)],
            "extra_instances": [repr(key) for key in sorted(actual_keys - expected_keys)],
            "missing_cell_types": dict(expected_cell_types - actual_cell_types),
            "extra_cell_types": dict(actual_cell_types - expected_cell_types),
            "missing_ports": sorted(expected_ports - actual_ports),
            "extra_ports": sorted(actual_ports - expected_ports),
            "missing_nets_preview": [
                compact_signature(signature) for signature in list(missing_signatures)[:10]
            ],
            "extra_nets_preview": [
                compact_signature(signature) for signature in list(extra_signatures)[:10]
            ],
        },
    }


def atomic_write_text(path: Path, payload: str) -> None:
    """Publish one artifact atomically and remove its temporary on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def clear_result_artifacts(*paths: Path) -> None:
    """Ensure a failed comparison cannot leave a cacheable success artifact."""
    for path in paths:
        path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "gds",
        type=Path,
        nargs="?",
        default=Path("puzzle.gds"),
        help="input GDS (default: puzzle.gds)",
    )
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=Path("build/puzzle_netlist_klayout.json"),
        help="independently extracted netlist JSON",
    )
    parser.add_argument(
        "--vendor-root", type=Path, default=Path("vendor/sky130_fd_sc_hd")
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=Path("build/puzzle_netlist.json"),
        help="reference netlist to compare modulo net names",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("build/klayout_crosscheck.json"),
        help="compact JSON comparison report",
    )
    args = parser.parse_args()

    output_paths = {args.output.resolve(), args.report.resolve()}
    if len(output_paths) != 2 or args.compare.resolve() in output_paths:
        raise SystemExit(
            "Independent netlist, comparison report, and reference netlist "
            "must be three distinct paths"
        )

    # These are derived build products.  Remove older copies before starting so
    # an import error, interrupted extraction, or comparison failure cannot be
    # mistaken for a current successful cross-check by Make.
    clear_result_artifacts(args.output, args.report)

    if kdb is None or klayout is None:
        raise SystemExit(
            "KLayout's Python module is required. Install it with "
            "`python -m pip install klayout` and make sure its native libraries "
            f"are loadable. Original error: {KLAYOUT_IMPORT_ERROR}"
        )

    netlist = extract(args.gds, args.vendor_root)
    reference = json.loads(args.compare.read_text())
    comparison_started = time.perf_counter()
    report = {
        "extractor": netlist["extractor"],
        "gds": str(args.gds),
        "reference": str(args.compare),
        "geometry": {
            "conductor_components": netlist["stats"]["conductor_components"],
            "via_cuts": netlist["stats"]["via_cuts"],
        },
        "timings_seconds": netlist["stats"]["timings_seconds"],
        **compare_netlists(netlist, reference, dbu=netlist["dbu"]),
    }
    report["timings_seconds"]["compare"] = (
        time.perf_counter() - comparison_started
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["matched"]:
        sys.exit(1)

    # Build both payloads completely before publishing either.  If either
    # atomic replacement fails, remove both results; the Make stamp is written
    # only after this process exits successfully and verifies both files.
    netlist_payload = json.dumps(netlist, indent=2, sort_keys=True) + "\n"
    report_payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    try:
        atomic_write_text(args.output, netlist_payload)
        atomic_write_text(args.report, report_payload)
    except BaseException:
        clear_result_artifacts(args.output, args.report)
        raise


if __name__ == "__main__":
    main()
