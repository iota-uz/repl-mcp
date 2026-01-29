#!/usr/bin/env python3
"""
Example 03: Namespace Reset

Demonstrates:
- Resetting namespace clears variables
- MCP object is preserved after reset
- Clean slate for new executions
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from repl_mcp.repl_engine import REPLEngine


class MockMCP:
    """Mock MCP object for testing."""
    def __repr__(self):
        return "<MockMCP>"


def test_reset_clears_variables():
    """Test that reset clears user variables."""
    print("Test 1: Reset clears variables...")

    mcp = MockMCP()
    engine = REPLEngine(mcp_wrapper=mcp)

    # Add some variables
    engine.execute("x = 42")
    engine.execute("y = 100")
    engine.execute("import json")

    # Verify variables exist
    vars_before = engine.get_namespace_vars()
    assert "x" in vars_before
    assert "y" in vars_before

    # Reset namespace
    engine.reset_namespace()

    # Verify variables are gone
    vars_after = engine.get_namespace_vars()
    assert "x" not in vars_after
    assert "y" not in vars_after
    assert "json" not in vars_after

    print("  ✓ Reset clears variables")


def test_reset_preserves_mcp():
    """Test that reset preserves mcp object."""
    print("\nTest 2: Reset preserves mcp...")

    mcp = MockMCP()
    engine = REPLEngine(mcp_wrapper=mcp)

    # Verify mcp is accessible
    result1 = engine.execute("mcp")
    assert result1.success
    assert "MockMCP" in result1.return_value

    # Add some variables and reset
    engine.execute("x = 42")
    engine.reset_namespace()

    # Verify mcp still accessible
    result2 = engine.execute("mcp")
    assert result2.success
    assert "MockMCP" in result2.return_value

    print("  ✓ MCP object preserved after reset")


def test_multiple_resets():
    """Test multiple resets in sequence."""
    print("\nTest 3: Multiple resets...")

    mcp = MockMCP()
    engine = REPLEngine(mcp_wrapper=mcp)

    for i in range(3):
        # Add variable
        engine.execute(f"x = {i}")
        assert engine.get_namespace_vars()["x"] == str(i)

        # Reset
        engine.reset_namespace()
        assert "x" not in engine.get_namespace_vars()

        # MCP still works
        result = engine.execute("mcp")
        assert result.success

    print("  ✓ Multiple resets work correctly")


def test_reset_with_execution():
    """Test that state is preserved without explicit reset."""
    print("\nTest 4: State preservation...")

    mcp = MockMCP()
    engine = REPLEngine(mcp_wrapper=mcp)

    # Set variables
    engine.execute("x = 1")
    engine.execute("y = 2")

    # Execute without reset should preserve previous variables
    result = engine.execute("z = 3")
    assert result.success
    vars_dict = result.namespace_vars
    assert "x" in vars_dict
    assert "y" in vars_dict
    assert "z" in vars_dict

    print("  ✓ State preserved across executions")


if __name__ == "__main__":
    print("=" * 60)
    print("Example 03: Namespace Reset")
    print("=" * 60)

    try:
        test_reset_clears_variables()
        test_reset_preserves_mcp()
        test_multiple_resets()
        test_reset_with_execution()

        print("\n" + "=" * 60)
        print("✓ All tests passed!")
        print("=" * 60)

    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
