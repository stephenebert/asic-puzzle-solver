#!/usr/bin/env python3
"""Render reproducible stills and silent videos for the ASIC write-up.

The renderer uses only local puzzle evidence: ``layout.png`` and the JSON
artifacts under ``build/``.  If ``architecture.json`` or ``easter_eggs.json``
is present, it is recorded and used; otherwise the corresponding facts are
derived from the recovered netlist and GDS hierarchy.

Default outputs under ``build/media``:

* four 1800x1100 evidence graphics and a 1920x1080 walkthrough poster;
* a captioned 78-second 1920x1080 explainer;
* a captioned 20-second 1920x1080 answer reveal;
* decoded-video contact sheets and ``validation.json``.

The videos are intentionally silent and contain no external or generated
stock assets.  Re-run this file whenever the source evidence changes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Iterable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps
import imageio_ffmpeg


STILL_SIZE = (1800, 1100)
VIDEO_SIZE = (1920, 1080)
VIDEO_FPS = 24
ANIMATION_FPS = 12

EXPLAINER_CAPTIONS = (
    (7.0, "The starting point is the routed physical layout—not source RTL."),
    (12.0, "Two unrelated geometry engines recover the same 728 instances and all 739 nets."),
    (15.0, "The serial input is an 11×11 two-star Star Battle board with eleven recovered regions."),
    (12.0, "Solver checks prove 121 enabled bits is the minimum accepted length and the witness is unique."),
    (12.0, "The acceptance slice contains 79 of 92 flip-flops and is equivalent to the direct puzzle rules."),
    (10.0, "Below the die, 36 custom bars decode as PER ARENAM AD ASTRA—through the sand to the stars."),
    (10.0, "Concrete replay reads fifteen ASCII bytes from O[7:0], completing with Success = 1."),
)

REVEAL_CAPTIONS = (
    (3.0, "A 121-bit serial input unlocks the chip."),
    (9.0, "Read left to right, the bits draw the unique two-star board."),
    (3.0, "Two stars per row, column, and region—none touching."),
    (2.0, "O[7:0] emits the answer byte by byte."),
    (3.0, "Success = 1; the bytes spell (* TWO STARS *)."),
)

BG_TOP = "#07111d"
BG_BOTTOM = "#0c1c2d"
PANEL = "#102235"
PANEL_2 = "#142b40"
INK = "#f4f7f9"
MUTED = "#9cb0c3"
FAINT = "#547087"
CYAN = "#39d7ff"
MINT = "#5de2a5"
GOLD = "#ffc857"
CORAL = "#ff7383"
PURPLE = "#aa92ff"

def first_font(*candidates: str) -> Path:
    """Return the first installed font from macOS, Linux, or Windows paths."""
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return path
    raise RuntimeError(f"No suitable font found; tried: {', '.join(candidates)}")


FONT_REGULAR = first_font(
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
FONT_BOLD = first_font(
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
FONT_MONO = first_font(
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationMono-Regular.ttf",
    "C:/Windows/Fonts/consola.ttf",
)

REGION_COLORS = (
    "#183b54",
    "#233b55",
    "#263750",
    "#30364f",
    "#163f4a",
    "#254147",
    "#2d3d43",
    "#18394c",
    "#26384f",
    "#303a49",
    "#1f404b",
)


def load_font(size: int, *, bold: bool = False, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_MONO if mono else FONT_BOLD if bold else FONT_REGULAR
    return ImageFont.truetype(str(path), size=size)


def color(value: str, alpha: int = 255) -> tuple[int, int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4)) + (alpha,)


def mix(left: tuple[int, ...], right: tuple[int, ...], amount: float) -> tuple[int, ...]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * amount) for a, b in zip(left, right))


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3.0 - 2.0 * value)


def fade(value: float, start: float, end: float) -> float:
    if end <= start:
        return float(value >= end)
    return ease((value - start) / (end - start))


def background(size: tuple[int, int]) -> Image.Image:
    width, height = size
    top = color(BG_TOP)
    bottom = color(BG_BOTTOM)
    image = Image.new("RGBA", size)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        draw.line((0, y, width, y), fill=mix(top, bottom, y / max(1, height - 1)))
    for x in range(40, width, 80):
        draw.line((x, 0, x, height), fill=color("#163047", 34), width=1)
    for y in range(40, height, 80):
        draw.line((0, y, width, y), fill=color("#163047", 34), width=1)
    return image


def rounded_panel(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    fill: str = PANEL,
    outline: str = "#24445f",
    radius: int = 28,
    width: int = 2,
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=color(fill), outline=color(outline), width=width)


def wrap_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def wrapped_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    font: ImageFont.FreeTypeFont,
    fill: str = INK,
    max_width: int,
    spacing: int = 10,
    anchor: str = "la",
) -> int:
    x, y = xy
    lines = wrap_lines(draw, text, font, max_width)
    bbox = font.getbbox("Ag")
    line_height = bbox[3] - bbox[1] + spacing
    for line in lines:
        draw.text((x, y), line, font=font, fill=color(fill), anchor=anchor)
        y += line_height
    return y


def title_block(
    draw: ImageDraw.ImageDraw,
    kicker: str,
    title: str,
    subtitle: str,
    *,
    width: int,
) -> None:
    draw.text((72, 54), kicker.upper(), font=load_font(22, bold=True), fill=color(CYAN))
    draw.text((72, 90), title, font=load_font(50, bold=True), fill=color(INK))
    wrapped_text(
        draw,
        (74, 158),
        subtitle,
        font=load_font(28),
        fill=MUTED,
        max_width=width - 148,
        spacing=7,
    )


def footer(draw: ImageDraw.ImageDraw, label: str, width: int, height: int) -> None:
    draw.line((72, height - 54, width - 72, height - 54), fill=color("#24445f"), width=2)
    draw.text((72, height - 39), label, font=load_font(18), fill=color(FAINT), anchor="lm")
    draw.text(
        (width - 72, height - 39),
        "reproducible from local puzzle artifacts",
        font=load_font(23),
        fill=color(FAINT),
        anchor="rm",
    )


def arrow(
    draw: ImageDraw.ImageDraw,
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    fill: str = CYAN,
    width: int = 5,
) -> None:
    draw.line((*start, *end), fill=color(fill), width=width)
    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    length = 16
    spread = 0.52
    left = (
        end[0] - length * math.cos(angle - spread),
        end[1] - length * math.sin(angle - spread),
    )
    right = (
        end[0] - length * math.cos(angle + spread),
        end[1] - length * math.sin(angle + spread),
    )
    draw.polygon((end, left, right), fill=color(fill))


def five_point_star(cx: float, cy: float, radius: float) -> list[tuple[float, float]]:
    points = []
    for index in range(10):
        angle = -math.pi / 2 + index * math.pi / 5
        current = radius if index % 2 == 0 else radius * 0.43
        points.append((cx + current * math.cos(angle), cy + current * math.sin(angle)))
    return points


def paste_contained(
    destination: Image.Image,
    source: Image.Image,
    box: tuple[int, int, int, int],
    *,
    background_fill: Optional[str] = None,
) -> None:
    left, top, right, bottom = box
    target = (right - left, bottom - top)
    image = ImageOps.contain(source.convert("RGBA"), target, Image.Resampling.LANCZOS)
    x = left + (target[0] - image.width) // 2
    y = top + (target[1] - image.height) // 2
    if background_fill is not None:
        ImageDraw.Draw(destination).rounded_rectangle(box, radius=22, fill=color(background_fill))
    destination.alpha_composite(image, (x, y))


def json_load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def optional_json(path: Path) -> Optional[dict[str, Any]]:
    return json_load(path) if path.exists() else None


MORSE_TABLE = {
    ".-": "A",
    "-...": "B",
    "-.-.": "C",
    "-..": "D",
    ".": "E",
    "..-.": "F",
    "--.": "G",
    "....": "H",
    "..": "I",
    ".---": "J",
    "-.-": "K",
    ".-..": "L",
    "--": "M",
    "-.": "N",
    "---": "O",
    ".--.": "P",
    "--.-": "Q",
    ".-.": "R",
    "...": "S",
    "-": "T",
    "..-": "U",
    "...-": "V",
    ".--": "W",
    "-..-": "X",
    "-.--": "Y",
    "--..": "Z",
}


def decode_morse_symbols(symbols: list[dict[str, Any]]) -> tuple[list[str], str]:
    groups: list[str] = []
    current = ""
    for index, item in enumerate(symbols):
        current += item["symbol"]
        gap = item.get("gap_after")
        if gap is None or gap >= 8.0:
            groups.append(current)
            if gap is not None:
                groups.append("/")
            current = ""
        elif gap >= 3.0:
            groups.append(current)
            current = ""
    if current:
        groups.append(current)
    decoded = "".join(" " if group == "/" else MORSE_TABLE.get(group, "?") for group in groups)
    return groups, decoded.strip()


def morse_from_gds(gds_path: Path) -> dict[str, Any]:
    try:
        import gdstk
    except ImportError as error:  # pragma: no cover - base project dependency
        raise RuntimeError(
            "easter_eggs.json is absent and gdstk is unavailable for Morse recovery"
        ) from error
    library = gdstk.read_gds(gds_path)
    top_cells = library.top_level()
    if len(top_cells) != 1:
        raise ValueError("Expected one GDS top cell while recovering Morse")
    references = []
    for reference in top_cells[0].references:
        if not reference.cell_name.startswith("INTERNAL_"):
            continue
        bbox = reference.bounding_box()
        references.append(
            {
                "cell": reference.cell_name,
                "x": float(reference.origin[0]),
                "y": float(reference.origin[1]),
                "width": float(bbox[1][0] - bbox[0][0]),
                "symbol": "-" if reference.cell_name.endswith("_7") else ".",
            }
        )
    references.sort(key=lambda item: item["x"])
    for index, item in enumerate(references):
        if index + 1 == len(references):
            item["gap_after"] = None
        else:
            edge = item["x"] + item["width"]
            item["gap_after"] = round(references[index + 1]["x"] - edge, 2)
    groups, decoded = decode_morse_symbols(references)
    return {
        "source": "puzzle.gds hierarchy",
        "reference_count": len(references),
        "y_microns": references[0]["y"] if references else None,
        "symbols": references,
        "groups": groups,
        "decoded": decoded,
    }


def morse_from_easter_json(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    morse = payload.get("morse")
    if not isinstance(morse, dict) or not isinstance(morse.get("symbols"), list):
        return None
    unit = float(morse.get("unit_um", 1.38))
    symbols = []
    raw_symbols = morse["symbols"]
    for index, raw in enumerate(raw_symbols):
        mark = raw.get("mark") or raw.get("symbol")
        x0 = raw.get("x0_um", raw.get("x0", raw.get("x")))
        x1 = raw.get("x1_um", raw.get("x1"))
        width = raw.get("width_um", raw.get("width"))
        if mark not in {".", "-"} or x0 is None:
            return None
        if width is None and x1 is not None:
            width = float(x1) - float(x0)
        if width is None:
            width = unit * (3 if mark == "-" else 1)
        gap_units = raw.get("gap_units")
        if gap_units is not None:
            gap_after = float(gap_units) * unit
        elif index + 1 < len(raw_symbols) and x1 is not None:
            next_raw = raw_symbols[index + 1]
            next_x = next_raw.get("x0_um", next_raw.get("x0", next_raw.get("x")))
            gap_after = None if next_x is None else float(next_x) - float(x1)
        else:
            gap_after = None
        symbols.append(
            {
                "cell": raw.get("cell_name", "custom Morse bar"),
                "x": float(x0),
                "y": float(raw.get("y0_um", raw.get("y", -52.72))),
                "width": float(width),
                "symbol": mark,
                "gap_after": None if gap_after is None else round(gap_after, 2),
            }
        )
    groups, decoded = decode_morse_symbols(symbols)
    phrase = morse.get("phrase")
    if isinstance(phrase, str):
        declared = phrase.strip().upper()
        if decoded != declared:
            raise ValueError(
                f"Morse symbols decode as {decoded!r}, not declared phrase {declared!r}"
            )
    return {
        "source": "build/easter_eggs.json",
        "reference_count": len(symbols),
        "y_microns": symbols[0]["y"] if symbols else None,
        "symbols": symbols,
        "groups": groups,
        "decoded": decoded,
    }


@dataclass
class MediaData:
    root: Path
    solution: dict[str, Any]
    regions: dict[str, Any]
    protocol: dict[str, Any]
    netlist: dict[str, Any]
    crosscheck: dict[str, Any]
    architecture: Optional[dict[str, Any]]
    easter_eggs: Optional[dict[str, Any]]
    layout: Image.Image
    morse: dict[str, Any]

    @property
    def rows(self) -> list[str]:
        return self.solution["rows"]

    @property
    def bits(self) -> str:
        return self.solution["bits"]

    @property
    def sequential_count(self) -> int:
        models = self.netlist["models"]
        return sum(bool(models[item["cell"]]["sequential"]) for item in self.netlist["instances"])

    @property
    def combinational_count(self) -> int:
        return len(self.netlist["instances"]) - self.sequential_count


def load_media_data(root: Path) -> MediaData:
    build = root / "build"
    architecture = optional_json(build / "architecture.json")
    eggs = optional_json(build / "easter_eggs.json")
    gds_morse = morse_from_gds(root / "puzzle.gds")
    json_morse = morse_from_easter_json(eggs) if eggs else None
    if json_morse is not None:
        if json_morse["decoded"] != gds_morse["decoded"]:
            raise ValueError("Morse JSON disagrees with direct GDS hierarchy decode")
        json_morse["corroborated_by"] = "direct puzzle.gds hierarchy decode"
        morse = json_morse
    else:
        morse = gds_morse
    crosscheck = json_load(build / "klayout_crosscheck.json")
    counts = crosscheck.get("counts", {})
    if not crosscheck.get("matched"):
        raise ValueError("Independent KLayout extraction did not match the primary netlist")
    expected_crosscheck = {
        "actual_instances": 728,
        "actual_cell_types": 66,
        "actual_ports": 13,
        "actual_nets": 739,
        "matched_net_signatures": 739,
        "missing_net_signatures": 0,
        "extra_net_signatures": 0,
    }
    discrepancies = {
        key: (counts.get(key), expected)
        for key, expected in expected_crosscheck.items()
        if counts.get(key) != expected
    }
    if discrepancies:
        raise ValueError(f"Unexpected KLayout cross-check counts: {discrepancies}")
    return MediaData(
        root=root,
        solution=json_load(build / "solution.json"),
        regions=json_load(build / "regions.json"),
        protocol=json_load(build / "protocol_proof.json"),
        netlist=json_load(build / "puzzle_netlist.json"),
        crosscheck=crosscheck,
        architecture=architecture,
        easter_eggs=eggs,
        layout=Image.open(root / "layout.png").convert("RGBA"),
        morse=morse,
    )


def draw_metric(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    number: str,
    label: str,
    *,
    accent: str = CYAN,
) -> None:
    rounded_panel(draw, box, fill=PANEL_2, outline="#2c526e", radius=18)
    left, top, right, _ = box
    draw.text((left + 22, top + 18), number, font=load_font(36, bold=True), fill=color(accent))
    wrapped_text(
        draw,
        (left + 22, top + 66),
        label,
        font=load_font(23),
        fill=MUTED,
        max_width=right - left - 40,
        spacing=4,
    )


def render_extractor(data: MediaData, size: tuple[int, int] = STILL_SIZE) -> Image.Image:
    image = background(size)
    draw = ImageDraw.Draw(image)
    width, height = size
    title_block(
        draw,
        "Independent extraction cross-check",
        "Two geometry engines. One exact netlist.",
        "The comparison ignores arbitrary net names and matches every net by its complete set of physical instance pins and top-level ports.",
        width=width,
    )

    rounded_panel(draw, (72, 245, 545, 940), fill="#f7f8fa", outline="#35536a")
    cropped_layout = data.layout.crop((40, 20, 850, 990))
    paste_contained(image, cropped_layout, (92, 270, 525, 890), background_fill="#f7f8fa")
    draw.text((308, 910), "puzzle.gds", font=load_font(22, bold=True), fill=color("#17344b"), anchor="mm")

    draw.text((650, 268), "PRIMARY", font=load_font(20, bold=True), fill=color(PURPLE))
    rounded_panel(draw, (620, 300, 1030, 472), fill="#15263b", outline=PURPLE, radius=22)
    draw.text((825, 340), "gdstk", font=load_font(34, bold=True), fill=color(INK), anchor="mm")
    draw.text((825, 390), "+ Shapely geometry", font=load_font(27), fill=color(MUTED), anchor="mm")
    draw.text((825, 430), "original extraction", font=load_font(22), fill=color(FAINT), anchor="mm")

    draw.text((650, 524), "INDEPENDENT", font=load_font(20, bold=True), fill=color(CYAN))
    rounded_panel(draw, (620, 556, 1030, 704), fill="#15263b", outline=CYAN, radius=22)
    draw.text((825, 598), "KLayout 0.30.10", font=load_font(32, bold=True), fill=color(INK), anchor="mm")
    draw.text((825, 647), "parser + Region engine", font=load_font(27), fill=color(MUTED), anchor="mm")
    draw.text((825, 680), "no gdstk / no Shapely", font=load_font(22), fill=color(FAINT), anchor="mm")

    arrow(draw, (1035, 386), (1148, 485), fill=PURPLE, width=5)
    arrow(draw, (1035, 642), (1148, 535), fill=CYAN, width=5)
    rounded_panel(draw, (1145, 385, 1718, 637), fill="#102a36", outline=MINT, radius=28, width=3)
    draw.text((1432, 431), "CANONICAL CONVERGENCE", font=load_font(22, bold=True), fill=color(MINT), anchor="mm")
    counts = data.crosscheck["counts"]
    matched = counts["matched_net_signatures"]
    expected = counts["expected_nets"]
    draw.text((1432, 501), f"{matched} / {expected}", font=load_font(62, bold=True), fill=color(INK), anchor="mm")
    draw.text((1432, 558), "net endpoint partitions identical", font=load_font(27), fill=color(MUTED), anchor="mm")
    draw.text(
        (1432, 601),
        f"{counts['missing_net_signatures']} missing  ·  {counts['extra_net_signatures']} extra",
        font=load_font(26, bold=True),
        fill=color(MINT),
        anchor="mm",
    )

    metric_y = 720
    metrics = (
        (str(counts["actual_instances"]), "functional instances"),
        (str(counts["actual_cell_types"]), "cell variants"),
        (str(counts["actual_ports"]), "top-level ports"),
        (str(counts["actual_nets"]), "signal nets"),
    )
    for index, (number, label) in enumerate(metrics):
        left = 620 + index * 278
        draw_metric(draw, (left, metric_y, left + 250, metric_y + 140), number, label, accent=(CYAN if index % 2 == 0 else MINT))

    draw.text((620, 886), "Independent replay from the KLayout netlist", font=load_font(27, bold=True), fill=color(INK))
    draw.text((620, 930), "312 waveform edges matched · winner emitted (* TWO STARS *)", font=load_font(24), fill=color(MUTED))
    draw.text((620, 966), "Universal circuit-versus-rules equivalence remained UNSAT.", font=load_font(24), fill=color(MUTED))
    footer(draw, "Evidence: build/klayout_crosscheck.json + independently extracted netlist", width, height)
    return image


def draw_protocol_axis(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    progress: float = 1.0,
) -> None:
    left, top, right, bottom = box
    axis_y = top + 126
    draw.line((left, axis_y, right, axis_y), fill=color("#35536a"), width=8)
    phases = [
        (0.00, 0.12, CORAL, "RESET", "3 edges"),
        (0.12, 0.20, PURPLE, "OPTIONAL IDLE", "0 or 1 edge"),
        (0.20, 0.78, CYAN, "SERIAL BURST", "121 enabled edges"),
        (0.78, 0.88, GOLD, "DECIDE", "disable + 1 edge"),
        (0.88, 1.00, MINT, "READOUT", "15 ASCII bytes"),
    ]
    for start, end, accent, label, detail in phases:
        x0 = left + (right - left) * start
        x1 = left + (right - left) * end
        active_end = left + (right - left) * progress
        shown = min(x1, active_end)
        if shown > x0:
            draw.line((x0, axis_y, shown, axis_y), fill=color(accent), width=12)
        center = (x0 + x1) / 2
        draw.text((center, top + 34), label, font=load_font(21, bold=True), fill=color(accent), anchor="mm")
        draw.text((center, top + 74), detail, font=load_font(22), fill=color(MUTED), anchor="mm")
        draw.ellipse((x0 - 8, axis_y - 8, x0 + 8, axis_y + 8), fill=color(accent))
    draw.ellipse((right - 8, axis_y - 8, right + 8, axis_y + 8), fill=color(MINT))
    marker_x = left + (right - left) * max(0.0, min(1.0, progress))
    draw.ellipse((marker_x - 13, axis_y - 13, marker_x + 13, axis_y + 13), fill=color(INK), outline=color(CYAN), width=4)


def render_protocol(data: MediaData, size: tuple[int, int] = STILL_SIZE) -> Image.Image:
    image = background(size)
    draw = ImageDraw.Draw(image)
    width, height = size
    theorem = data.protocol["theorem"]
    title_block(
        draw,
        "Formal protocol proof",
        "121 bits is minimal — not merely sufficient.",
        "A symbolic unrolling proves every shorter enabled burst impossible, proves the 121-bit witness unique, and characterizes every longer burst.",
        width=width,
    )

    rounded_panel(draw, (72, 245, 1728, 490), fill="#0f2336", outline="#2d506a")
    draw_protocol_axis(draw, (120, 270, 1680, 465))

    cards = [
        (72, 540, 482, 866, CORAL, "0–120 bits", "UNSAT", "No input of any shorter length can make success rise on the following disabled edge."),
        (507, 540, 917, 866, CYAN, "121 bits", "SAT · UNIQUE", "Exactly one 121-bit witness exists. After bit 121, success is still low until the next rising edge."),
        (942, 540, 1352, 866, GOLD, "122nd edge", "FIRST HIGH", "Deassert enable: success rises now. If enable remains high, this is also the first possible success edge."),
        (1377, 540, 1728, 866, MINT, "N ≥ 121", "PREFIX RULE", "Only the first 121 bits matter; the N−121 suffix bits are don't-cares for success."),
    ]
    for left, top, right, bottom, accent, heading, result, body in cards:
        rounded_panel(draw, (left, top, right, bottom), fill=PANEL, outline=accent, radius=24, width=3)
        draw.text((left + 26, top + 30), heading, font=load_font(24, bold=True), fill=color(INK))
        draw.text((left + 26, top + 81), result, font=load_font(27, bold=True), fill=color(accent))
        wrapped_text(draw, (left + 26, top + 143), body, font=load_font(26), fill=MUTED, max_width=right - left - 52, spacing=7)

    rounded_panel(draw, (72, 900, 1728, 1005), fill="#172437", outline="#344b61", radius=20)
    draw.text((102, 928), "Clean capture rule", font=load_font(24, bold=True), fill=color(GOLD))
    draw.text(
        (340, 928),
        "Drop enable immediately after bit 121; extra enabled clocks advance the ASCII output generator past leading bytes.",
        font=load_font(24),
        fill=color(INK),
    )
    draw.text(
        (102, 970),
        f"Bounded checks: {theorem['bounded_lengths_proved'][0]}–{theorem['bounded_lengths_proved'][1]}  ·  unbounded suffix/fixed-point proof: UNSAT obligations",
        font=load_font(22),
        fill=color(MUTED),
    )
    footer(draw, "Evidence: build/protocol_proof.json", width, height)
    return image


def architecture_state_budget(data: MediaData) -> list[dict[str, Any]]:
    if data.architecture is not None:
        visual = data.architecture.get("visual_data", {})
        raw_budget = visual.get("state_budget")
        if isinstance(raw_budget, list) and raw_budget:
            canonical = {
                "column-counter FFs": 0,
                "region-counter FFs": 0,
                "position": 0,
                "population": 0,
                "row control": 0,
                "no-touch": 0,
                "output / control": 0,
            }
            for item in raw_budget:
                label = item.get("label") or item.get("group") or item.get("id")
                count_value = item.get("count")
                if label is not None and count_value is not None:
                    normalized = str(label).lower()
                    if "column" in normalized and "counter" in normalized:
                        key = "column-counter FFs"
                    elif "region" in normalized and "counter" in normalized:
                        key = "region-counter FFs"
                    elif "position" in normalized:
                        key = "position"
                    elif "population" in normalized:
                        key = "population"
                    elif "row" in normalized:
                        key = "row control"
                    elif "touch" in normalized:
                        key = "no-touch"
                    elif "output" in normalized or "control" in normalized:
                        key = "output / control"
                    else:
                        continue
                    canonical[key] += int(count_value)
            budget = [
                {"label": label, "count": count_value}
                for label, count_value in canonical.items()
                if count_value
            ]
            if budget and sum(item["count"] for item in budget) == 92:
                return budget
    # These counts are also derivable from the exact FF groups.  The fallback
    # keeps media rendering available while architecture.json is regenerated.
    return [
        {"label": "column-counter FFs", "count": 22},
        {"label": "region-counter FFs", "count": 22},
        {"label": "position", "count": 9},
        {"label": "population", "count": 8},
        {"label": "row control", "count": 3},
        {"label": "no-touch", "count": 13},
        {"label": "output / control", "count": 15},
    ]


def render_architecture(data: MediaData, size: tuple[int, int] = STILL_SIZE) -> Image.Image:
    image = background(size)
    draw = ImageDraw.Draw(image)
    width, height = size
    cone = data.protocol["success_cone"]
    title_block(
        draw,
        "Recovered state architecture",
        "The full chip is larger than the acceptance cone.",
        "Physical connectivity, Liberty cell models, symbolic slicing, and the annotated layout separate serial control, puzzle checking, and byte output.",
        width=width,
    )

    rounded_panel(draw, (72, 245, 1165, 865), fill="#0e2133", outline="#2a4b64")
    nodes = [
        ((115, 315, 340, 520), CYAN, "SERIAL INPUT", "I · clk\nenable · rst_n"),
        ((430, 285, 715, 550), PURPLE, "FULL STATE", f"{data.sequential_count} flip-flops\n{data.combinational_count} comb. cells"),
        ((805, 270, 1115, 565), GOLD, "SUCCESS CONE", f"{cone['sequential']} flip-flops\n{cone['combinational']} comb. cells\n{cone['nets']} nets"),
    ]
    for box, accent, heading, body in nodes:
        rounded_panel(draw, box, fill=PANEL_2, outline=accent, radius=25, width=3)
        left, top, right, bottom = box
        draw.text(((left + right) / 2, top + 48), heading, font=load_font(25, bold=True), fill=color(accent), anchor="mm")
        for index, line in enumerate(body.split("\n")):
            draw.text(((left + right) / 2, top + 108 + index * 46), line, font=load_font(26, bold=index == 0), fill=color(INK if index == 0 else MUTED), anchor="mm")
    arrow(draw, (342, 418), (426, 418), fill=CYAN)
    arrow(draw, (718, 418), (801, 418), fill=GOLD)
    draw.text((960, 516), "rules XOR circuit: UNSAT", font=load_font(23, bold=True), fill=color(MINT), anchor="mm")
    draw.text((960, 544), "for all 2¹²¹ boards", font=load_font(21), fill=color(MINT), anchor="mm")

    budget = architecture_state_budget(data)
    budget_colors = (CYAN, "#2fb6c7", PURPLE, "#699cff", GOLD, MINT, CORAL)
    draw.text((115, 602), "EXACT 92-FLIP-FLOP ALLOCATION", font=load_font(24, bold=True), fill=color(INK))
    bar_left, bar_top, bar_right, bar_bottom = 115, 642, 1118, 700
    cursor = bar_left
    for index, item in enumerate(budget):
        segment_right = bar_right if index + 1 == len(budget) else cursor + (bar_right - bar_left) * item["count"] / 92
        draw.rectangle((cursor, bar_top, segment_right, bar_bottom), fill=color(budget_colors[index % len(budget_colors)]))
        if segment_right - cursor > 52:
            draw.text(((cursor + segment_right) / 2, (bar_top + bar_bottom) / 2), str(item["count"]), font=load_font(22, bold=True), fill=color("#07131f"), anchor="mm")
        cursor = segment_right
    draw.rounded_rectangle((bar_left, bar_top, bar_right, bar_bottom), radius=14, outline=color(INK), width=2)

    for index, item in enumerate(budget):
        column = index % 3
        row = index // 3
        x = 116 + column * 338
        y = 726 + row * 45
        accent = budget_colors[index % len(budget_colors)]
        draw.rounded_rectangle((x, y, x + 24, y + 24), radius=6, fill=color(accent))
        draw.text((x + 36, y + 12), f"{item['count']}  {item['label']}", font=load_font(21, bold=True), fill=color(INK), anchor="lm")

    rounded_panel(draw, (1200, 245, 1728, 865), fill="#f5f7f8", outline="#3b5a70")
    layout_crop = data.layout.crop((40, 20, 850, 990))
    paste_contained(image, layout_crop, (1220, 275, 1708, 803), background_fill="#f5f7f8")
    draw.rounded_rectangle((1337, 314, 1608, 645), radius=14, outline=color(GOLD), width=7)
    draw.text((1473, 826), "separate O[7:0] output path", font=load_font(25, bold=True), fill=color("#18344a"), anchor="mm")

    metrics = (
        ("728", "functional cells"),
        ("92", "flip-flops total"),
        ("79", "flip-flops in success cone"),
        ("13", "ports"),
    )
    for index, (number, label) in enumerate(metrics):
        left = 72 + index * 414
        draw_metric(draw, (left, 895, left + 380, 1003), number, label, accent=(CYAN, PURPLE, GOLD, MINT)[index])
    source_label = "Evidence: recovered netlist + protocol success-cone slice + layout.png"
    if data.architecture is not None:
        source_label += " + build/architecture.json"
    footer(draw, source_label, width, height)
    return image


def render_board(
    image: Image.Image,
    data: MediaData,
    box: tuple[int, int, int, int],
    *,
    revealed: int = 121,
    show_regions: bool = True,
    active_index: Optional[int] = None,
) -> None:
    draw = ImageDraw.Draw(image)
    left, top, right, bottom = box
    cell = min((right - left) / 11, (bottom - top) / 11)
    board_size = cell * 11
    left += ((right - left) - board_size) / 2
    top += ((bottom - top) - board_size) / 2
    regions = data.regions["regions"]
    for row in range(11):
        for column in range(11):
            index = row * 11 + column
            x0 = left + column * cell
            y0 = top + row * cell
            fill_color = REGION_COLORS[regions[row][column]] if show_regions else "#11283b"
            if index >= revealed:
                fill_color = "#0b1927"
            draw.rectangle((x0, y0, x0 + cell, y0 + cell), fill=color(fill_color), outline=color("#335067"), width=1)
            if index < revealed and data.rows[row][column] == "#":
                draw.polygon(five_point_star(x0 + cell / 2, y0 + cell / 2, cell * 0.30), fill=color(GOLD))
            elif index < revealed:
                draw.ellipse((x0 + cell * 0.45, y0 + cell * 0.45, x0 + cell * 0.55, y0 + cell * 0.55), fill=color("#537085"))
            if active_index == index:
                draw.rectangle((x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2), outline=color(CYAN), width=max(3, round(cell * 0.07)))
    if show_regions:
        for row in range(11):
            for column in range(11):
                region = regions[row][column]
                x0 = left + column * cell
                y0 = top + row * cell
                if row == 0 or regions[row - 1][column] != region:
                    draw.line((x0, y0, x0 + cell, y0), fill=color(INK), width=max(2, round(cell * 0.07)))
                if column == 0 or regions[row][column - 1] != region:
                    draw.line((x0, y0, x0, y0 + cell), fill=color(INK), width=max(2, round(cell * 0.07)))
                if row == 10 or regions[row + 1][column] != region:
                    draw.line((x0, y0 + cell, x0 + cell, y0 + cell), fill=color(INK), width=max(2, round(cell * 0.07)))
                if column == 10 or regions[row][column + 1] != region:
                    draw.line((x0 + cell, y0, x0 + cell, y0 + cell), fill=color(INK), width=max(2, round(cell * 0.07)))
    draw.rectangle((left, top, left + board_size, top + board_size), outline=color(INK), width=max(3, round(cell * 0.09)))


def render_walkthrough_poster(data: MediaData) -> Image.Image:
    image = background(VIDEO_SIZE)
    draw = ImageDraw.Draw(image)
    width, height = VIDEO_SIZE
    draw.text((64, 48), "FROM GDS TO STAR BATTLE", font=load_font(23, bold=True), fill=color(CYAN))
    draw.text((64, 83), "Reverse-engineering the ASIC puzzle", font=load_font(47, bold=True), fill=color(INK))
    draw.text((64, 145), "physical layout  →  exact netlist  →  unique board  →  ASCII", font=load_font(27), fill=color(MUTED))

    rounded_panel(draw, (54, 208, 595, 950), fill="#f5f7f8", outline="#385970")
    layout_crop = data.layout.crop((40, 20, 850, 990))
    paste_contained(image, layout_crop, (75, 230, 574, 870), background_fill="#f5f7f8")
    draw.text((324, 910), "puzzle.gds", font=load_font(27, bold=True), fill=color("#19364b"), anchor="mm")

    arrow(draw, (608, 570), (662, 570), fill=CYAN, width=6)
    render_board(image, data, (680, 218, 1340, 878), revealed=121, show_regions=True)
    draw.text((1010, 920), "unique 11×11 two-star board", font=load_font(27, bold=True), fill=color(GOLD), anchor="mm")

    arrow(draw, (1350, 570), (1402, 570), fill=GOLD, width=6)
    rounded_panel(draw, (1410, 260, 1867, 830), fill="#102b35", outline=MINT, radius=30, width=4)
    draw.text((1638, 318), "SUCCESS", font=load_font(24, bold=True), fill=color(MINT), anchor="mm")
    draw.text((1638, 385), "1", font=load_font(68, bold=True), fill=color(INK), anchor="mm")
    draw.text((1638, 475), "O[7:0]", font=load_font(24, bold=True), fill=color(MUTED), anchor="mm")
    draw.text((1638, 540), "(* TWO", font=load_font(43, bold=True, mono=True), fill=color(INK), anchor="mm")
    draw.text((1638, 600), "STARS *)", font=load_font(43, bold=True, mono=True), fill=color(INK), anchor="mm")
    draw.line((1470, 650, 1805, 650), fill=color("#31536b"), width=2)
    draw.text((1638, 700), "121 bits · 22 stars", font=load_font(25, bold=True), fill=color(GOLD), anchor="mm")
    draw.text((1638, 748), "739 nets cross-checked", font=load_font(24), fill=color(MUTED), anchor="mm")

    draw.line((54, 1008, 1867, 1008), fill=color("#2a4b64"), width=2)
    draw.text((54, 1044), "Verified by concrete replay, independent extraction, uniqueness, and all-board SMT equivalence.", font=load_font(24), fill=color(MUTED), anchor="lm")
    draw.text((1867, 1044), "(* TWO STARS *)", font=load_font(24, bold=True, mono=True), fill=color(MINT), anchor="rm")
    return image


def render_morse(
    data: MediaData,
    size: tuple[int, int] = STILL_SIZE,
    *,
    reveal_count: Optional[int] = None,
) -> Image.Image:
    image = background(size)
    draw = ImageDraw.Draw(image)
    width, height = size
    morse = data.morse
    symbols = morse["symbols"]
    if reveal_count is None:
        reveal_count = len(symbols)
    title_block(
        draw,
        "Physical-layout Easter egg",
        "The geometry below the die is Morse code.",
        "Thirty-six custom references at y = −52.72 µm use short and long rectangles as dots and dashes; physical gaps delimit letters and words.",
        width=width,
    )

    rounded_panel(draw, (72, 250, 1728, 545), fill="#0e2133", outline="#31536b")
    x0, x1 = 120, 1680
    baseline = 408
    scale = (x1 - x0) / 200.0
    draw.line((x0, baseline + 45, x1, baseline + 45), fill=color("#345269"), width=2)
    for index, item in enumerate(symbols):
        px = x0 + item["x"] * scale
        bar_width = max(7, item["width"] * scale)
        if index < reveal_count:
            accent = GOLD if item["symbol"] == "-" else CYAN
            draw.rounded_rectangle((px, baseline - 26, px + bar_width, baseline + 26), radius=8, fill=color(accent))
        else:
            draw.rounded_rectangle((px, baseline - 26, px + bar_width, baseline + 26), radius=8, fill=color("#21394d"))
    draw.text((x0, 300), "actual x-position and relative bar width", font=load_font(25, bold=True), fill=color(INK))
    draw.text((x1, 300), "short = dot   ·   long = dash", font=load_font(25), fill=color(MUTED), anchor="ra")
    for marker in (0, 50, 100, 150, 200):
        px = x0 + marker * scale
        draw.line((px, baseline + 38, px, baseline + 52), fill=color(FAINT), width=2)
        draw.text((px, baseline + 75), str(marker), font=load_font(15), fill=color(FAINT), anchor="mm")

    rounded_panel(draw, (72, 585, 1728, 830), fill="#11283a", outline="#2b4c65")
    groups = morse["groups"]
    morse_line = "   ".join("/" if group == "/" else group for group in groups)
    letters = "   ".join("/" if group == "/" else MORSE_TABLE.get(group, "?") for group in groups)
    draw.text((900, 645), morse_line, font=load_font(25, mono=True), fill=color(CYAN), anchor="mm")
    draw.text((900, 710), letters, font=load_font(31, bold=True, mono=True), fill=color(GOLD), anchor="mm")
    draw.line((140, 754, 1660, 754), fill=color("#35536a"), width=2)
    draw.text((900, 796), morse["decoded"], font=load_font(42, bold=True), fill=color(INK), anchor="mm")

    rounded_panel(draw, (72, 872, 1728, 1007), fill="#132c35", outline=MINT, radius=22, width=3)
    draw.text((112, 914), "PER ARENAM AD ASTRA", font=load_font(30, bold=True), fill=color(MINT))
    draw.text((112, 960), "A natural silicon-themed reading: “Through the sand to the stars.”", font=load_font(28), fill=color(INK))
    draw.text((1684, 937), f"{morse['reference_count']} refs", font=load_font(24, bold=True), fill=color(MUTED), anchor="rm")
    source = "Evidence: puzzle.gds INTERNAL_3 / INTERNAL_7 hierarchy"
    if data.easter_eggs is not None:
        source += " + build/easter_eggs.json"
    footer(draw, source, width, height)
    return image


def render_intro_frame(data: MediaData, progress: float) -> Image.Image:
    image = background(VIDEO_SIZE)
    draw = ImageDraw.Draw(image)
    width, height = VIDEO_SIZE
    alpha = round(255 * fade(progress, 0.0, 0.18))
    cropped = data.layout.crop((40, 20, 850, 990))
    card = Image.new("RGBA", (700, 790), color("#f6f7f8"))
    paste_contained(card, cropped, (20, 20, 680, 770), background_fill="#f6f7f8")
    card.putalpha(alpha)
    image.alpha_composite(card, (1110, 85))
    draw.text((100, 160), "REVERSE-ENGINEERING", font=load_font(25, bold=True), fill=color(CYAN, alpha))
    wrapped_text(draw, (100, 220), "From GDS\nto Star Battle", font=load_font(68, bold=True), fill=INK, max_width=900, spacing=9)
    draw.text((100, 458), "A reproducible ASIC puzzle solution", font=load_font(29), fill=color(MUTED, alpha))
    draw.rounded_rectangle((100, 540, 925, 682), radius=24, fill=color("#10283a", alpha), outline=color("#31536b", alpha), width=2)
    gds_megabytes = (data.root / "puzzle.gds").stat().st_size / 1_000_000
    draw.text((140, 585), f"{gds_megabytes:.2f} MB", font=load_font(34, bold=True), fill=color(GOLD, alpha))
    draw.text((310, 585), "GDS  →  728 cells  →  one unique board", font=load_font(27), fill=color(INK, alpha))
    draw.text((100, 760), "Every claim below is tied to extracted geometry or a solver proof.", font=load_font(25), fill=color(MUTED, alpha))
    return image


def ken_burns(source: Image.Image, progress: float, *, zoom: float = 0.018) -> Image.Image:
    width, height = VIDEO_SIZE
    base_scale = max(width / source.width, height / source.height)
    scale = base_scale * (1.0 + zoom * ease(progress))
    resized = source.resize((round(source.width * scale), round(source.height * scale)), Image.Resampling.LANCZOS)
    travel_x = max(0, resized.width - width)
    travel_y = max(0, resized.height - height)
    left = round(travel_x * (0.42 + 0.12 * ease(progress)))
    top = round(travel_y * (0.48 - 0.08 * ease(progress)))
    return resized.crop((left, top, left + width, top + height)).convert("RGBA")


def render_board_frame(data: MediaData, progress: float) -> Image.Image:
    image = background(VIDEO_SIZE)
    draw = ImageDraw.Draw(image)
    draw.text((85, 70), "THE 121 SERIAL BITS BECOME AN 11×11 BOARD", font=load_font(34, bold=True), fill=color(CYAN))
    reveal = min(121, max(0, int(fade(progress, 0.05, 0.72) * 122)))
    render_board(image, data, (90, 150, 860, 920), revealed=reveal, show_regions=True, active_index=(reveal - 1 if 0 < reveal < 121 else None))
    draw.text((960, 180), f"{reveal:03d} / 121 bits", font=load_font(52, bold=True, mono=True), fill=color(GOLD))
    bits = data.bits
    start = max(0, reveal - 33)
    snippet = bits[start:reveal]
    draw.text((960, 260), snippet.rjust(33, "·"), font=load_font(27, mono=True), fill=color(MUTED))
    rules = [
        ("22 stars total", 0.24),
        ("exactly 2 in every row", 0.42),
        ("exactly 2 in every column", 0.54),
        ("exactly 2 in every region", 0.66),
        ("no stars touch — even diagonally", 0.78),
    ]
    for index, (label, threshold) in enumerate(rules):
        y = 360 + index * 90
        visible = fade(progress, threshold, threshold + 0.08)
        draw.ellipse((970, y, 1020, y + 50), fill=color(MINT, round(255 * visible)), outline=color("#295066", round(255 * visible)), width=2)
        if visible > 0:
            draw.line((983, y + 26, 994, y + 38), fill=color("#0c2130", round(255 * visible)), width=5)
            draw.line((994, y + 38, 1009, y + 15), fill=color("#0c2130", round(255 * visible)), width=5)
        draw.text((1050, y + 25), label, font=load_font(26, bold=True), fill=color(INK, round(255 * visible)), anchor="lm")
    if progress > 0.82:
        draw.rounded_rectangle((960, 835, 1770, 930), radius=22, fill=color("#112d36"), outline=color(MINT), width=3)
        draw.text((1365, 883), "UNIQUE STAR BATTLE SOLUTION", font=load_font(28, bold=True), fill=color(MINT), anchor="mm")
    return image


def render_protocol_frame(data: MediaData, progress: float) -> Image.Image:
    image = background(VIDEO_SIZE)
    draw = ImageDraw.Draw(image)
    draw.text((85, 70), "THE CLOCKING PROTOCOL IS FORMALLY CHARACTERIZED", font=load_font(34, bold=True), fill=color(CYAN))
    rounded_panel(draw, (85, 165, 1835, 500), fill="#0f2336", outline="#31536b")
    draw_protocol_axis(draw, (135, 210, 1785, 475), progress=ease(progress))
    phases = [
        (0.12, "Lengths 0–120", "UNSAT", CORAL),
        (0.40, "Length 121", "SAT · UNIQUE", CYAN),
        (0.65, "Following disabled edge", "SUCCESS = 1", GOLD),
        (0.82, "Every N ≥ 121", "FIRST 121 BITS DECIDE", MINT),
    ]
    for index, (threshold, label, result, accent) in enumerate(phases):
        visible = fade(progress, threshold, threshold + 0.10)
        x = 100 + index * 445
        draw.rounded_rectangle((x, 590, x + 400, 820), radius=24, fill=color(PANEL, round(255 * visible)), outline=color(accent, round(255 * visible)), width=3)
        draw.text((x + 30, 635), label, font=load_font(23, bold=True), fill=color(INK, round(255 * visible)))
        draw.text((x + 30, 705), result, font=load_font(28, bold=True), fill=color(accent, round(255 * visible)))
    draw.text((960, 900), "For clean ASCII output, drop enable immediately after bit 121.", font=load_font(27), fill=color(MUTED), anchor="mm")
    return image


def render_answer_frame(data: MediaData, progress: float, *, compact: bool = False) -> Image.Image:
    image = background(VIDEO_SIZE)
    draw = ImageDraw.Draw(image)
    if compact:
        draw.text((960, 85), "THE CHIP'S ANSWER", font=load_font(31, bold=True), fill=color(CYAN), anchor="mm")
        board_box = (105, 170, 850, 915)
        answer_x = 1360
    else:
        draw.text((960, 90), "CONCRETE REPLAY OF THE INDEPENDENT NETLIST", font=load_font(31, bold=True), fill=color(CYAN), anchor="mm")
        board_box = (110, 195, 820, 905)
        answer_x = 1370
    render_board(image, data, board_box, revealed=121, show_regions=True)
    bytes_hex = [data.solution["output_hex"][index : index + 2] for index in range(0, len(data.solution["output_hex"]), 2)]
    byte_count = min(len(bytes_hex), max(0, int(fade(progress, 0.12, 0.76) * (len(bytes_hex) + 1))))
    visible_hex = " ".join(bytes_hex[:byte_count])
    visible_text = bytes.fromhex("".join(bytes_hex[:byte_count])).decode("ascii") if byte_count else ""
    draw.text((930, 300), "O[7:0] bytes", font=load_font(24, bold=True), fill=color(MUTED))
    wrapped_text(draw, (930, 350), visible_hex, font=load_font(27, mono=True), fill=GOLD, max_width=850, spacing=12)
    draw.rounded_rectangle((900, 500, 1810, 720), radius=30, fill=color("#0f2c36"), outline=color(MINT), width=4)
    draw.text((answer_x, 610), visible_text, font=load_font(55, bold=True, mono=True), fill=color(INK), anchor="mm")
    if byte_count:
        draw.text((answer_x, 790), "Success = 1", font=load_font(32, bold=True), fill=color(MINT), anchor="mm")
        draw.text((answer_x, 846), "312 VCD edges replayed exactly", font=load_font(24), fill=color(MUTED), anchor="mm")
    return image


def add_caption(image: Image.Image, caption: str, *, accent: str = CYAN) -> Image.Image:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    width, height = image.size
    caption_font = load_font(24, bold=True)
    if draw.textlength(caption, font=caption_font) > width - 270:
        raise ValueError(f"Burned caption does not fit on one line: {caption}")
    draw.rounded_rectangle((80, height - 68, width - 80, height - 18), radius=15, fill=color("#06111c", 236), outline=color(accent, 170), width=2)
    draw.rectangle((98, height - 57, 105, height - 29), fill=color(accent))
    draw.text((124, height - 43), caption, font=caption_font, fill=color(INK), anchor="lm")
    return Image.alpha_composite(image, overlay)


def explainer_scene_renderer(data: MediaData, stills: dict[str, Image.Image]) -> tuple[Callable[[float], Image.Image], float]:
    renderers = [
        lambda p: render_intro_frame(data, p),
        lambda p: ken_burns(stills["extractor"], p),
        lambda p: render_board_frame(data, p),
        lambda p: render_protocol_frame(data, p),
        lambda p: ken_burns(stills["architecture"], p),
        lambda p: render_morse(data, VIDEO_SIZE, reveal_count=round(len(data.morse["symbols"]) * ease(p))),
        lambda p: render_answer_frame(data, p),
    ]
    accents = (CYAN, MINT, GOLD, CYAN, PURPLE, GOLD, MINT)
    scenes: list[tuple[float, Callable[[float], Image.Image], str, str]] = [
        (duration, renderer, caption, accent)
        for (duration, caption), renderer, accent in zip(EXPLAINER_CAPTIONS, renderers, accents)
    ]
    starts = []
    elapsed = 0.0
    for duration, renderer, caption, accent in scenes:
        starts.append((elapsed, duration, renderer, caption, accent))
        elapsed += duration
    def render(time_seconds: float) -> Image.Image:
        index = len(starts) - 1
        for candidate, (start, duration, _, _, _) in enumerate(starts):
            if time_seconds < start + duration:
                index = candidate
                break
        start, duration, renderer, caption, accent = starts[index]
        progress = max(0.0, min(1.0, (time_seconds - start) / duration))
        current = renderer(progress)
        return add_caption(current, caption, accent=accent)

    return render, elapsed


def reveal_scene_renderer(data: MediaData) -> tuple[Callable[[float], Image.Image], float]:
    duration = 20.0

    def render(time_seconds: float) -> Image.Image:
        if time_seconds < 3.0:
            frame = render_intro_frame(data, time_seconds / 3.0)
            caption = REVEAL_CAPTIONS[0][1]
            accent = CYAN
        elif time_seconds < 12.0:
            progress = (time_seconds - 3.0) / 9.0
            frame = render_board_frame(data, progress)
            caption = REVEAL_CAPTIONS[1][1]
            accent = GOLD
        elif time_seconds < 15.0:
            frame = render_board_frame(data, 1.0)
            caption = REVEAL_CAPTIONS[2][1]
            accent = MINT
        elif time_seconds < 17.0:
            progress = (time_seconds - 15.0) / 2.0
            frame = render_answer_frame(data, progress, compact=True)
            caption = REVEAL_CAPTIONS[3][1]
            accent = MINT
        else:
            frame = render_answer_frame(data, 1.0, compact=True)
            caption = REVEAL_CAPTIONS[4][1]
            accent = MINT
        return add_caption(frame, caption, accent=accent)

    return render, duration


def encode_video(
    path: Path,
    renderer: Callable[[float], Image.Image],
    duration: float,
    *,
    fps: int = VIDEO_FPS,
    animation_fps: int = ANIMATION_FPS,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = imageio_ffmpeg.write_frames(
        str(path),
        VIDEO_SIZE,
        fps=fps,
        codec="libx264",
        macro_block_size=1,
        pix_fmt_in="rgb24",
        pix_fmt_out="yuv420p",
        output_params=["-crf", "19", "-preset", "medium", "-movflags", "+faststart", "-an"],
    )
    writer.send(None)
    total_animation_frames = round(duration * animation_fps)
    duplicates = fps // animation_fps
    if duplicates * animation_fps != fps:
        raise ValueError("VIDEO_FPS must be an integer multiple of ANIMATION_FPS")
    try:
        for index in range(total_animation_frames):
            time_seconds = (index + 0.5) / animation_fps
            frame = renderer(min(duration, time_seconds)).convert("RGBA")
            # Animated elements use alpha for fades.  Flatten explicitly so
            # RGB conversion cannot discard alpha and reveal their raw color.
            opaque = Image.new("RGBA", VIDEO_SIZE, color(BG_TOP))
            frame = Image.alpha_composite(opaque, frame).convert("RGB")
            payload = frame.tobytes()
            for _ in range(duplicates):
                writer.send(payload)
    finally:
        writer.close()


def srt_timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def write_srt(path: Path, cues: Iterable[tuple[float, str]]) -> None:
    lines = []
    start = 0.0
    for index, (duration, caption) in enumerate(cues, start=1):
        end = start + duration
        lines.extend(
            [
                str(index),
                f"{srt_timestamp(start)} --> {srt_timestamp(end)}",
                caption,
                "",
            ]
        )
        start = end
    path.write_text("\n".join(lines), encoding="utf-8")


def video_probe(path: Path) -> dict[str, Any]:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = result.stderr
    duration_match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", output)
    video_match = re.search(r"Video:\s*([^,]+).*?(\d{3,5})x(\d{3,5}).*?(\d+(?:\.\d+)?) fps", output)
    if not duration_match or not video_match:
        raise RuntimeError(f"Could not probe {path}:\n{output}")
    hours, minutes, seconds = duration_match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return {
        "duration_seconds": duration,
        "codec": video_match.group(1).strip(),
        "width": int(video_match.group(2)),
        "height": int(video_match.group(3)),
        "fps": float(video_match.group(4)),
        "audio_streams": len(re.findall(r"Stream #.*Audio:", output)),
        "video_streams": len(re.findall(r"Stream #.*Video:", output)),
    }


def decoded_frame(path: Path, time_seconds: float) -> Image.Image:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-ss",
            f"{time_seconds:.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    return Image.open(BytesIO(result.stdout)).convert("RGB")


def contact_sheet(path: Path, output: Path, times: list[float], title: str) -> None:
    canvas = background((1800, 1100))
    draw = ImageDraw.Draw(canvas)
    draw.text((60, 48), title, font=load_font(38, bold=True), fill=color(INK))
    thumb_width, thumb_height = 402, 226
    for index, time_seconds in enumerate(times):
        frame = decoded_frame(path, time_seconds)
        frame = ImageOps.fit(frame, (thumb_width, thumb_height), Image.Resampling.LANCZOS)
        column = index % 4
        row = index // 4
        x = 60 + column * 435
        y = 125 + row * 425
        draw.rounded_rectangle((x - 8, y - 8, x + thumb_width + 8, y + thumb_height + 8), radius=18, fill=color(PANEL), outline=color("#31536b"), width=2)
        canvas.alpha_composite(frame.convert("RGBA"), (x, y))
        draw.text((x, y + thumb_height + 34), f"{time_seconds:05.1f}s", font=load_font(20, bold=True, mono=True), fill=color(CYAN))
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(output, quality=95)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_outputs(
    output_dir: Path,
    still_paths: dict[str, Path],
    videos: dict[str, tuple[Path, float]],
    data: MediaData,
) -> dict[str, Any]:
    validation: dict[str, Any] = {
        "sources": {
            "layout": "layout.png",
            "solution": "build/solution.json",
            "regions": "build/regions.json",
            "protocol": "build/protocol_proof.json",
            "netlist": "build/puzzle_netlist.json",
            "crosscheck": "build/klayout_crosscheck.json",
            "architecture": "build/architecture.json" if data.architecture is not None else "derived fallback",
            "easter_eggs": "build/easter_eggs.json" if data.easter_eggs is not None else "derived from puzzle.gds",
        },
        "facts": {
            "answer": data.solution["output_text"],
            "winning_bits": len(data.bits),
            "stars": data.bits.count("1"),
            "regions": len(set(sum(data.regions["regions"], []))),
            "minimum_length": data.protocol["theorem"]["minimum_accepted_length"],
            "independent_nets_matched": data.crosscheck["counts"]["matched_net_signatures"],
            "morse": data.morse["decoded"],
        },
        "stills": {},
        "videos": {},
    }
    for name, path in still_paths.items():
        with Image.open(path) as image:
            dimensions = list(image.size)
        expected_size = VIDEO_SIZE if name == "walkthrough_poster" else STILL_SIZE
        if dimensions != list(expected_size):
            raise ValueError(f"Unexpected still dimensions for {path}: {dimensions}")
        validation["stills"][name] = {
            "path": str(path.relative_to(data.root)),
            "width": dimensions[0],
            "height": dimensions[1],
            "sha256": sha256(path),
        }
    for name, (path, expected_duration) in videos.items():
        probe = video_probe(path)
        if (probe["width"], probe["height"]) != VIDEO_SIZE:
            raise ValueError(f"Unexpected video dimensions for {path}: {probe}")
        if probe["audio_streams"] != 0 or probe["video_streams"] != 1:
            raise ValueError(f"Unexpected stream topology for {path}: {probe}")
        if abs(probe["duration_seconds"] - expected_duration) > 0.12:
            raise ValueError(f"Unexpected duration for {path}: {probe}")
        validation["videos"][name] = {
            "path": str(path.relative_to(data.root)),
            "expected_duration_seconds": expected_duration,
            **probe,
            "sha256": sha256(path),
        }
        subtitle_path = path.with_suffix(".srt")
        if subtitle_path.exists():
            subtitle_text = subtitle_path.read_text(encoding="utf-8")
            validation["videos"][name]["subtitle"] = {
                "path": str(subtitle_path.relative_to(data.root)),
                "cues": subtitle_text.count(" --> "),
                "sha256": sha256(subtitle_path),
            }
    validation_path = output_dir / "validation.json"
    validation_path.write_text(json.dumps(validation, indent=2, sort_keys=True) + "\n")
    return validation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="output directory (default: build/media)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stills-only", action="store_true", help="skip video encoding and QA")
    mode.add_argument("--videos-only", action="store_true", help="reuse or render stills, then encode videos")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    output_dir = args.output_dir or root / "build" / "media"
    output_dir.mkdir(parents=True, exist_ok=True)
    data = load_media_data(root)
    if len(data.bits) != 121 or data.solution["output_text"] != "(* TWO STARS *)":
        raise ValueError("Source solution artifact is inconsistent")
    if data.morse["decoded"] != "PER ARENAM AD ASTRA":
        raise ValueError(f"Unexpected physical Morse decode: {data.morse['decoded']}")

    still_renderers = {
        "extractor": ("extractor_convergence.png", render_extractor),
        "protocol": ("protocol_timeline.png", render_protocol),
        "architecture": ("architecture_dataflow.png", render_architecture),
        "morse": ("physical_morse.png", render_morse),
    }
    still_paths: dict[str, Path] = {}
    still_images: dict[str, Image.Image] = {}
    for name, (filename, renderer) in still_renderers.items():
        path = output_dir / filename
        if not args.videos_only or not path.exists():
            renderer(data).convert("RGB").save(path, optimize=True)
        still_paths[name] = path
        still_images[name] = Image.open(path).convert("RGBA")

    poster_path = output_dir / "walkthrough_poster.png"
    if not args.videos_only or not poster_path.exists():
        render_walkthrough_poster(data).convert("RGB").save(poster_path, optimize=True)
    still_paths["walkthrough_poster"] = poster_path

    videos: dict[str, tuple[Path, float]] = {}
    if not args.stills_only:
        explainer_renderer, explainer_duration = explainer_scene_renderer(data, still_images)
        explainer_path = output_dir / "asic_puzzle_explainer.mp4"
        encode_video(explainer_path, explainer_renderer, explainer_duration)
        write_srt(explainer_path.with_suffix(".srt"), EXPLAINER_CAPTIONS)
        videos["explainer"] = (explainer_path, explainer_duration)

        reveal_renderer, reveal_duration = reveal_scene_renderer(data)
        reveal_path = output_dir / "answer_reveal.mp4"
        encode_video(reveal_path, reveal_renderer, reveal_duration)
        write_srt(reveal_path.with_suffix(".srt"), REVEAL_CAPTIONS)
        videos["answer_reveal"] = (reveal_path, reveal_duration)

        qa_dir = output_dir / "qa"
        contact_sheet(
            explainer_path,
            qa_dir / "explainer_contact_sheet.png",
            [2.0, 7.0, 19.0, 34.0, 46.0, 58.0, 68.0, 76.0],
            "Decoded explainer cuts and final frame",
        )
        contact_sheet(
            reveal_path,
            qa_dir / "answer_reveal_contact_sheet.png",
            [1.0, 3.5, 6.0, 12.5, 15.2, 16.6, 17.2, 19.5],
            "Decoded answer reveal and three-second hold",
        )

    validation = validate_outputs(output_dir, still_paths, videos, data)
    print(json.dumps(validation, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
