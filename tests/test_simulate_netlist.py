#!/usr/bin/env python3
"""Focused tests for the Liberty expression evaluator."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from simulate_netlist import Expression, logic_and, logic_or, logic_xor  # noqa: E402


class ExpressionTests(unittest.TestCase):
    def test_operator_precedence(self) -> None:
        expression = Expression("A | B & !C ^ D")
        self.assertTrue(
            expression.evaluate({"A": False, "B": True, "C": False, "D": False})
        )
        self.assertFalse(
            expression.evaluate({"A": False, "B": True, "C": False, "D": True})
        )

    def test_unknown_logic_is_pessimistic(self) -> None:
        self.assertFalse(logic_and(False, None))
        self.assertIsNone(logic_and(True, None))
        self.assertTrue(logic_or(True, None))
        self.assertIsNone(logic_or(False, None))
        self.assertIsNone(logic_xor(True, None))

    def test_invalid_expression_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unclosed parenthesis"):
            Expression("A & (B | C")


if __name__ == "__main__":
    unittest.main()
