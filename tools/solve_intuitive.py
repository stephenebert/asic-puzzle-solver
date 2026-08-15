#!/usr/bin/env python3
"""Solve the recovered Star Battle with transparent exhaustive backtracking.

This solver deliberately uses no SAT/SMT package.  It starts from the region
map recovered by ``recover_regions.py``, tries every legal two-star row pattern,
and prunes only choices that cannot possibly lead to a valid board.  After the
search proves the board unique, the candidate is replayed through the complete
gate-level circuit to recover the ASCII answer.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from simulate_netlist import Simulator


STARS_PER_UNIT = 2
PUZZLE_SERIAL_BITS = 121


@dataclass(frozen=True)
class RowChoice:
    """One legal row: exactly two stars with a gap between them."""

    mask: int
    columns: tuple[int, int]
    region_increments: tuple[int, ...]


@dataclass
class SearchStatistics:
    """Small counters that make the exhaustive search easy to audit."""

    states_visited: int = 0
    branches_considered: int = 0
    pruned_touching: int = 0
    pruned_overflow: int = 0
    pruned_capacity: int = 0
    cache_hits: int = 0


@dataclass
class SearchResult:
    """Boards found before the requested limit, plus proof metadata."""

    boards: list[tuple[int, ...]]
    search_exhausted: bool
    dead_states_cached: int
    statistics: SearchStatistics


def normalize_regions(payload: dict[str, Any]) -> tuple[tuple[int, ...], ...]:
    """Validate a square region grid and renumber its labels from zero."""

    if not isinstance(payload, dict):
        raise ValueError("region JSON must be an object")
    raw_regions = payload.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("region JSON must contain a non-empty 'regions' grid")

    size = len(raw_regions)
    if any(not isinstance(row, list) or len(row) != size for row in raw_regions):
        raise ValueError(f"region grid must be square; expected {size} cells per row")

    label_numbers: dict[Any, int] = {}
    normalized_rows = []
    for row in raw_regions:
        normalized_row = []
        for label in row:
            try:
                label_numbers.setdefault(label, len(label_numbers))
            except TypeError as error:
                raise ValueError(f"region label is not hashable: {label!r}") from error
            normalized_row.append(label_numbers[label])
        normalized_rows.append(tuple(normalized_row))

    if len(label_numbers) != size:
        raise ValueError(f"expected {size} regions, found {len(label_numbers)}")

    return tuple(normalized_rows)


def generate_row_patterns(size: int) -> tuple[int, ...]:
    """Return every two-star row with no horizontal contact."""

    patterns = []
    for left_column in range(size):
        for right_column in range(left_column + 2, size):
            patterns.append((1 << left_column) | (1 << right_column))
    return tuple(patterns)


class StarBattleSolver:
    """Enumerate all boards allowed by the recovered Star Battle rules."""

    def __init__(self, regions: tuple[tuple[int, ...], ...]):
        self.regions = regions
        self.size = len(regions)
        self.patterns = generate_row_patterns(self.size)
        self.choices_by_row = self._build_row_choices()
        self.region_suffix_capacity = self._build_region_suffix_capacity()

        self.statistics = SearchStatistics()
        self.dead_states: set[tuple[Any, ...]] = set()
        self.solutions: list[tuple[int, ...]] = []
        self.solution_limit = 2
        self.stopped_early = False

    def _build_row_choices(self) -> tuple[tuple[RowChoice, ...], ...]:
        choices_by_row = []
        for row_index, region_row in enumerate(self.regions):
            row_choices = []
            for mask in self.patterns:
                columns = tuple(
                    column for column in range(self.size) if mask & (1 << column)
                )
                if len(columns) != STARS_PER_UNIT:
                    raise AssertionError("row pattern does not contain exactly two stars")

                increments = [0] * self.size
                for column in columns:
                    increments[region_row[column]] += 1
                row_choices.append(
                    RowChoice(
                        mask=mask,
                        columns=(columns[0], columns[1]),
                        region_increments=tuple(increments),
                    )
                )
            choices_by_row.append(tuple(row_choices))
        return tuple(choices_by_row)

    def _build_region_suffix_capacity(self) -> tuple[tuple[int, ...], ...]:
        """Count region cells at or below each row for sound capacity pruning."""

        suffix = [[0] * self.size for _ in range(self.size + 1)]
        for row_index in reversed(range(self.size)):
            suffix[row_index] = list(suffix[row_index + 1])
            for region in self.regions[row_index]:
                suffix[row_index][region] += 1
        return tuple(tuple(counts) for counts in suffix)

    def solve(self, max_solutions: int = 2) -> SearchResult:
        """Search completely unless enough boards are found to disprove uniqueness."""

        if max_solutions < 1:
            raise ValueError("max_solutions must be positive")

        self.statistics = SearchStatistics()
        self.dead_states.clear()
        self.solutions.clear()
        self.solution_limit = max_solutions
        self.stopped_early = False

        zeros = (0,) * self.size
        self._search(
            row_index=0,
            previous_mask=0,
            column_counts=zeros,
            region_counts=zeros,
            chosen_rows=(),
        )
        return SearchResult(
            boards=list(self.solutions),
            search_exhausted=not self.stopped_early,
            dead_states_cached=len(self.dead_states),
            statistics=self.statistics,
        )

    def _search(
        self,
        row_index: int,
        previous_mask: int,
        column_counts: tuple[int, ...],
        region_counts: tuple[int, ...],
        chosen_rows: tuple[int, ...],
    ) -> bool:
        """Return whether this subtree contains at least one complete board."""

        if len(self.solutions) >= self.solution_limit:
            self.stopped_early = True
            return True

        state_key = (row_index, previous_mask, column_counts, region_counts)
        if state_key in self.dead_states:
            self.statistics.cache_hits += 1
            return False

        self.statistics.states_visited += 1
        if row_index == self.size:
            complete = all(count == STARS_PER_UNIT for count in column_counts) and all(
                count == STARS_PER_UNIT for count in region_counts
            )
            if complete:
                self.solutions.append(chosen_rows)
                return True
            self.dead_states.add(state_key)
            return False

        found_solution = False
        touching_columns = previous_mask | (previous_mask << 1) | (previous_mask >> 1)
        rows_left = self.size - row_index - 1
        remaining_region_cells = self.region_suffix_capacity[row_index + 1]

        for choice in self.choices_by_row[row_index]:
            if len(self.solutions) >= self.solution_limit:
                self.stopped_early = True
                return True

            self.statistics.branches_considered += 1
            if choice.mask & touching_columns:
                self.statistics.pruned_touching += 1
                continue

            next_columns = list(column_counts)
            for column in choice.columns:
                next_columns[column] += 1
            next_regions = tuple(
                count + increment
                for count, increment in zip(region_counts, choice.region_increments)
            )

            if any(count > STARS_PER_UNIT for count in next_columns) or any(
                count > STARS_PER_UNIT for count in next_regions
            ):
                self.statistics.pruned_overflow += 1
                continue

            # Each remaining row can add at most one star to a given column.
            if any(count + rows_left < STARS_PER_UNIT for count in next_columns):
                self.statistics.pruned_capacity += 1
                continue

            # A region cannot reach two if too few of its cells remain unvisited.
            if any(
                count + remaining_region_cells[region] < STARS_PER_UNIT
                for region, count in enumerate(next_regions)
            ):
                self.statistics.pruned_capacity += 1
                continue

            child_found = self._search(
                row_index=row_index + 1,
                previous_mask=choice.mask,
                column_counts=tuple(next_columns),
                region_counts=next_regions,
                chosen_rows=chosen_rows + (choice.mask,),
            )
            found_solution = found_solution or child_found

        # Only failed states are memoized.  Caching a successful suffix could
        # hide a second full board reached through a different prefix.
        if not found_solution and not self.stopped_early:
            self.dead_states.add(state_key)
        return found_solution


def board_to_bits(board: tuple[int, ...], size: int) -> str:
    return "".join(
        "1" if row_mask & (1 << column) else "0"
        for row_mask in board
        for column in range(size)
    )


def board_to_rows(board: tuple[int, ...], size: int) -> list[str]:
    return [
        "".join("#" if row_mask & (1 << column) else "." for column in range(size))
        for row_mask in board
    ]


def validate_board(
    board: tuple[int, ...], regions: tuple[tuple[int, ...], ...]
) -> None:
    """Check the solved board independently of the search implementation."""

    size = len(regions)
    if len(board) != size:
        raise AssertionError(f"board has {len(board)} rows instead of {size}")
    if any(bin(mask).count("1") != STARS_PER_UNIT for mask in board):
        raise AssertionError("a board row does not contain exactly two stars")

    for column in range(size):
        count = sum(bool(mask & (1 << column)) for mask in board)
        if count != STARS_PER_UNIT:
            raise AssertionError(f"column {column} contains {count} stars")

    for region in range(size):
        count = sum(
            bool(board[row] & (1 << column))
            for row in range(size)
            for column in range(size)
            if regions[row][column] == region
        )
        if count != STARS_PER_UNIT:
            raise AssertionError(f"region {region} contains {count} stars")

    for row in range(size):
        for column in range(size):
            if not board[row] & (1 << column):
                continue
            for row_delta, column_delta in ((0, 1), (1, -1), (1, 0), (1, 1)):
                other_row = row + row_delta
                other_column = column + column_delta
                if (
                    0 <= other_row < size
                    and 0 <= other_column < size
                    and board[other_row] & (1 << other_column)
                ):
                    raise AssertionError(
                        f"stars touch at ({row}, {column}) and "
                        f"({other_row}, {other_column})"
                    )


def replay_candidate(netlist: dict[str, Any], bit_string: str) -> tuple[bool, bytes]:
    """Clock a candidate through the full circuit and collect output bytes."""

    if len(bit_string) != PUZZLE_SERIAL_BITS:
        raise ValueError(
            f"candidate must contain exactly {PUZZLE_SERIAL_BITS} serial bits"
        )
    invalid_characters = set(bit_string) - {"0", "1"}
    if invalid_characters:
        invalid = "".join(sorted(invalid_characters))
        raise ValueError(
            f"candidate contains characters other than 0 and 1: {invalid!r}"
        )

    simulator = Simulator(netlist)
    simulator.set_inputs(clk=False, rst_n=False, enable=False, I=False)
    for _ in range(3):
        simulator.tick()

    simulator.set_inputs(rst_n=True)
    simulator.tick()
    simulator.set_inputs(enable=True)
    for index, bit in enumerate(bit_string, start=1):
        simulator.set_inputs(I=bit == "1")
        simulator.tick()
        if simulator.output("success") is True:
            raise AssertionError(f"success rose early on enabled bit {index}")

    simulator.set_inputs(enable=False)
    simulator.tick()
    success = simulator.output("success") is True
    first_byte = simulator.output_byte()
    if first_byte is None:
        raise AssertionError("first output byte contains unknown bits")

    output = bytearray()
    if first_byte:
        output.append(first_byte)
    for _ in range(255):
        simulator.tick()
        byte = simulator.output_byte()
        if byte is None:
            raise AssertionError("output contains unknown bits")
        if byte == 0:
            break
        output.append(byte)
    else:
        raise AssertionError("output generator did not terminate with a zero byte")

    return success, bytes(output)


def solve_and_replay(
    netlist: dict[str, Any],
    regions: tuple[tuple[int, ...], ...],
    max_solutions: int = 2,
) -> tuple[dict[str, Any], SearchResult, float]:
    solver = StarBattleSolver(regions)
    started = perf_counter()
    result = solver.solve(max_solutions=max_solutions)
    elapsed = perf_counter() - started

    if not result.boards:
        raise RuntimeError("the recovered Star Battle has no solution")

    board = result.boards[0]
    validate_board(board, regions)
    bit_string = board_to_bits(board, len(regions))
    success, output = replay_candidate(netlist, bit_string)
    if not success:
        raise AssertionError("the intuitive solution did not raise circuit success")

    unique = len(result.boards) == 1 and result.search_exhausted
    alternate_bits = (
        None
        if len(result.boards) == 1
        else board_to_bits(result.boards[1], len(regions))
    )
    payload = {
        "bits": bit_string,
        "rows": board_to_rows(board, len(regions)),
        "success": success,
        "unique": unique,
        "alternate_bits": alternate_bits,
        "output_hex": output.hex(),
        "output_text": output.decode("ascii"),
    }
    return payload, result, elapsed


def print_summary(
    payload: dict[str, Any], result: SearchResult, elapsed: float, row_patterns: int
) -> None:
    stats = result.statistics
    print("Intuitive Star Battle search")
    print(f"  legal two-star row patterns: {row_patterns}")
    print(f"  search states visited:       {stats.states_visited}")
    print(f"  candidate branches checked: {stats.branches_considered}")
    print(f"  dead states cached:         {result.dead_states_cached}")
    print(f"  solutions found:           {len(result.boards)}")
    print(f"  search exhausted:          {str(result.search_exhausted).lower()}")
    print(f"  elapsed:                   {elapsed:.3f}s")
    print()
    for row in payload["rows"]:
        print(row)
    print()
    print(f"Unique:  {str(payload['unique']).lower()}")
    print(f"Success: {str(payload['success']).lower()}")
    print(f"Output:  {payload['output_text']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path, help="recovered puzzle netlist JSON")
    parser.add_argument(
        "--regions",
        type=Path,
        default=Path("build/regions.json"),
        help="recovered region map (default: build/regions.json)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/solution_intuitive.json"),
        help="solution JSON to write (default: build/solution_intuitive.json)",
    )
    parser.add_argument(
        "--max-solutions",
        type=int,
        default=2,
        help="stop after this many boards; use at least 2 to test uniqueness",
    )
    args = parser.parse_args()
    if args.max_solutions < 1:
        parser.error("--max-solutions must be positive")

    netlist = json.loads(args.netlist.read_text())
    region_payload = json.loads(args.regions.read_text())
    regions = normalize_regions(region_payload)
    payload, result, elapsed = solve_and_replay(
        netlist, regions, max_solutions=args.max_solutions
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print_summary(payload, result, elapsed, len(generate_row_patterns(len(regions))))
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
