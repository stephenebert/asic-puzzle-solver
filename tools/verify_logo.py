#!/usr/bin/env python3
"""Verify the floating metal-2 logo in the puzzle and warm-up layouts."""

from __future__ import annotations

import argparse
import hashlib
import json
from math import isclose
from pathlib import Path
from typing import Any

import gdstk
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree


ROOT = Path(__file__).resolve().parent.parent
MET2_LAYER = 69
DRAWING_DATATYPE = 20
VIA_DATATYPE = 44
PIN_TEXTTYPE = 5
PIXEL_UM = 0.3
GRID_SIZE = 57
EXPECTED_PIXELS = 1366
EXPECTED_COMPONENT_AREAS_UM2 = (27.0, 42.3, 53.64)
EXPECTED_BITMAP_SHA256 = (
    "3c89f9334c37187264692e530b95f5f2809510750a6c5ed935afa186391ea020"
)
EXPECTED_BOUNDS = {
    "puzzle": (34.9, 35.2, 52.0, 52.3),
    "warmup": (65.9, 66.2, 83.0, 83.3),
}
ABS_TOLERANCE = 1e-8
LOGO_COLOR = "#173f73"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def polygon_geometry(polygon: Any) -> Polygon:
    geometry = Polygon(polygon.points)
    require(geometry.is_valid, "GDS polygon is not a valid planar geometry")
    return geometry


def is_logo_pixel(geometry: Polygon) -> bool:
    min_x, min_y, max_x, max_y = geometry.bounds
    return (
        len(geometry.exterior.coords) == 5
        and isclose(max_x - min_x, PIXEL_UM, abs_tol=ABS_TOLERANCE)
        and isclose(max_y - min_y, PIXEL_UM, abs_tol=ABS_TOLERANCE)
        and isclose(
            geometry.area,
            PIXEL_UM * PIXEL_UM,
            abs_tol=ABS_TOLERANCE,
        )
    )


def components(geometry: Any) -> list[Any]:
    if geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    return [geometry]


def close_tuple(actual: tuple[float, ...], expected: tuple[float, ...]) -> bool:
    return len(actual) == len(expected) and all(
        isclose(left, right, abs_tol=ABS_TOLERANCE)
        for left, right in zip(actual, expected)
    )


def inspect_layout(path: Path, expected_bounds: tuple[float, ...]) -> dict[str, Any]:
    library = gdstk.read_gds(path)
    top_cells = library.top_level()
    require(len(top_cells) == 1, f"Expected one top-level cell in {path}")
    top = top_cells[0]

    metal2 = [
        polygon_geometry(polygon)
        for polygon in top.get_polygons(
            layer=MET2_LAYER,
            datatype=DRAWING_DATATYPE,
        )
    ]
    pixels = [geometry for geometry in metal2 if is_logo_pixel(geometry)]
    require(
        len(pixels) == EXPECTED_PIXELS,
        f"Expected {EXPECTED_PIXELS} logo pixels in {path}, found {len(pixels)}",
    )

    lower_left = {(geometry.bounds[0], geometry.bounds[1]) for geometry in pixels}
    require(
        len(lower_left) == EXPECTED_PIXELS,
        f"Logo pixels in {path} do not have distinct lower-left coordinates",
    )
    origin_x = min(x for x, _ in lower_left)
    origin_y = min(y for _, y in lower_left)
    occupancy: set[tuple[int, int]] = set()
    for x, y in lower_left:
        column = round((x - origin_x) / PIXEL_UM)
        row = round((y - origin_y) / PIXEL_UM)
        require(
            isclose(x, origin_x + column * PIXEL_UM, abs_tol=ABS_TOLERANCE)
            and isclose(y, origin_y + row * PIXEL_UM, abs_tol=ABS_TOLERANCE),
            f"Logo pixel at {(x, y)} in {path} does not lie on the lattice",
        )
        require(
            0 <= column < GRID_SIZE and 0 <= row < GRID_SIZE,
            f"Logo pixel at {(x, y)} in {path} lies outside the grid",
        )
        occupancy.add((column, row))

    require(
        max(column for column, _ in occupancy) == GRID_SIZE - 1
        and max(row for _, row in occupancy) == GRID_SIZE - 1,
        f"Logo in {path} does not span a {GRID_SIZE} x {GRID_SIZE} grid",
    )
    bitmap = "".join(
        "1" if (column, row) in occupancy else "0"
        for row in range(GRID_SIZE)
        for column in range(GRID_SIZE)
    )
    bitmap_sha256 = hashlib.sha256(bitmap.encode("ascii")).hexdigest()
    require(
        bitmap_sha256 == EXPECTED_BITMAP_SHA256,
        f"Logo bitmap hash changed in {path}: {bitmap_sha256}",
    )

    logo = unary_union(pixels)
    logo_components = components(logo)
    component_areas = tuple(sorted(round(item.area, 8) for item in logo_components))
    require(
        close_tuple(component_areas, EXPECTED_COMPONENT_AREAS_UM2),
        f"Unexpected logo component areas in {path}: {component_areas}",
    )
    require(
        close_tuple(tuple(logo.bounds), expected_bounds),
        f"Unexpected logo bounds in {path}: {logo.bounds}",
    )

    other_metal2 = [geometry for geometry in metal2 if not is_logo_pixel(geometry)]
    metal_hits = STRtree(other_metal2).query(logo, predicate="intersects")
    require(
        len(metal_hits) == 0,
        f"Logo in {path} touches {len(metal_hits)} other metal-2 polygons",
    )

    via_hits = 0
    for lower_layer in (MET2_LAYER - 1, MET2_LAYER):
        for cut in top.get_polygons(layer=lower_layer, datatype=VIA_DATATYPE):
            if polygon_geometry(cut).intersects(logo):
                via_hits += 1
    require(via_hits == 0, f"Logo in {path} touches {via_hits} via cuts")

    label_hits = sum(
        Point(label.origin).intersects(logo)
        for label in top.get_labels(
            layer=MET2_LAYER,
            texttype=PIN_TEXTTYPE,
        )
    )
    require(label_hits == 0, f"Logo in {path} contains {label_hits} metal-2 pin labels")

    try:
        displayed_path = str(path.resolve().relative_to(ROOT.resolve()))
    except ValueError:
        displayed_path = str(path)

    return {
        "path": displayed_path,
        "top_cell": top.name,
        "bounds_um": [round(value, 8) for value in logo.bounds],
        "occupancy": sorted([column, row] for column, row in occupancy),
        "bitmap_sha256": bitmap_sha256,
        "component_areas_um2": list(component_areas),
        "routed_met2_intersections": len(metal_hits),
        "via_intersections": via_hits,
        "met2_pin_label_intersections": label_hits,
    }


def write_logo_svg(occupancy: set[tuple[int, int]], output: Path) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
            f'{GRID_SIZE} {GRID_SIZE}" width="1140" height="1140" '
            'shape-rendering="crispEdges" role="img" '
            'aria-labelledby="title description">'
        ),
        "  <title id=\"title\">Jane Street logo recovered from the GDS layout</title>",
        (
            "  <desc id=\"description\">A 57 by 57 grid reconstructed from "
            "floating metal-2 artwork.</desc>"
        ),
        f'  <rect width="{GRID_SIZE}" height="{GRID_SIZE}" fill="#ffffff"/>',
        f'  <g fill="{LOGO_COLOR}">',
    ]
    for column, row in sorted(occupancy, key=lambda point: (-point[1], point[0])):
        svg_row = GRID_SIZE - 1 - row
        lines.append(f'    <rect x="{column}" y="{svg_row}" width="1" height="1"/>')
    lines.extend(["  </g>", "</svg>"])

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--puzzle", type=Path, default=ROOT / "puzzle.gds")
    parser.add_argument(
        "--warmup",
        type=Path,
        default=ROOT / "warmup/04_final.gds",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build/logo_report.json",
    )
    parser.add_argument(
        "--svg",
        type=Path,
        default=ROOT / "build/jane_street_logo.svg",
    )
    args = parser.parse_args()

    puzzle = inspect_layout(args.puzzle, EXPECTED_BOUNDS["puzzle"])
    warmup = inspect_layout(args.warmup, EXPECTED_BOUNDS["warmup"])
    require(
        puzzle["occupancy"] == warmup["occupancy"],
        "Puzzle and warm-up logo bitmaps differ after translation",
    )

    write_logo_svg(
        {tuple(point) for point in puzzle["occupancy"]},
        args.svg,
    )

    for result in (puzzle, warmup):
        result.pop("occupancy")
    payload = {
        "schema_version": 1,
        "layer": MET2_LAYER,
        "datatype": DRAWING_DATATYPE,
        "pixel_size_um": PIXEL_UM,
        "pixel_count": EXPECTED_PIXELS,
        "grid": [GRID_SIZE, GRID_SIZE],
        "connected_components": len(EXPECTED_COMPONENT_AREAS_UM2),
        "normalized_patterns_equal": True,
        "bitmap_sha256": EXPECTED_BITMAP_SHA256,
        "rendered_svg": str(args.svg),
        "puzzle": puzzle,
        "warmup": warmup,
        "conclusion": (
            "Three floating metal-2 artwork components made from 1,366 square pixels"
        ),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(args.output)
    print(
        "Verified a shared 57 x 57 logo with 1,366 pixels and "
        "three floating components"
    )
    print(f"Wrote {args.output}")
    print(f"Wrote {args.svg}")


if __name__ == "__main__":
    main()
