#!/usr/bin/env python3
"""
Example 01: Basic Execution and State Persistence

Demonstrates:
- Basic code execution
- State persistence across executions
- Output capture (stdout, stderr, return values)
- Namespace introspection
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from repl_mcp.repl_engine import REPLEngine


def test_basic_execution():
    """Test basic Python execution."""
    print("Test 1: Basic execution...")

    engine = REPLEngine()
    result = engine.execute("x = 42")

    assert result.success, "Execution should succeed"
    assert "x" in result.namespace_vars, "Variable should be in namespace"
    assert result.namespace_vars["x"] == "42", "Variable should have correct value"
    print("  ✓ Basic execution works")


def test_state_persistence():
    """Test that variables persist across executions."""
    print("\nTest 2: State persistence...")

    engine = REPLEngine()

    # First execution
    result1 = engine.execute("x = 100")
    assert result1.success

    # Second execution uses previous variable
    result2 = engine.execute("y = x + 50")
    assert result2.success
    assert result2.namespace_vars["y"] == "150"
    print("  ✓ State persists across executions")


def test_output_capture():
    """Test stdout and return value capture."""
    print("\nTest 3: Output capture...")

    engine = REPLEngine()

    # Test stdout
    result = engine.execute('print("Hello, World!")')
    assert result.success
    assert result.stdout.strip() == "Hello, World!"
    print("  ✓ Stdout captured correctly")

    # Test return value
    result = engine.execute("42 + 8")
    assert result.success
    assert result.return_value == "50"
    print("  ✓ Return value captured correctly")


def test_multiline_code():
    """Test multiline code with functions."""
    print("\nTest 4: Multiline code...")

    engine = REPLEngine()

    code = """
def greet(name):
    return f"Hello, {name}!"

result = greet("World")
result
"""

    result = engine.execute(code)
    assert result.success
    assert result.return_value == "'Hello, World!'"
    assert "greet" in result.namespace_vars
    print("  ✓ Multiline code with functions works")


def test_imports():
    """Test that imports work and persist."""
    print("\nTest 5: Imports...")

    engine = REPLEngine()

    result1 = engine.execute("import json")
    assert result1.success

    result2 = engine.execute('json.dumps({"key": "value"})')
    assert result2.success
    assert "key" in result2.return_value
    print("  ✓ Imports work and persist")


if __name__ == "__main__":
    print("=" * 60)
    print("Example 01: Basic Execution and State Persistence")
    print("=" * 60)

    try:
        test_basic_execution()
        test_state_persistence()
        test_output_capture()
        test_multiline_code()
        test_imports()

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
