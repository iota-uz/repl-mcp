#!/usr/bin/env python3
"""
Example 02: Error Handling

Demonstrates:
- Exception handling and recovery
- Syntax error detection
- Namespace integrity after errors
- Error details in output
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from repl_mcp.repl_engine import REPLEngine


def test_runtime_error():
    """Test that runtime errors are captured correctly."""
    print("Test 1: Runtime error handling...")

    engine = REPLEngine()

    # Set up some state
    engine.execute("x = 42")

    # Execute code that raises exception
    result = engine.execute("y = 1 / 0")

    assert not result.success, "Execution should fail"
    assert result.exception is not None, "Should have exception info"
    assert result.exception.type == "ZeroDivisionError"
    assert "division by zero" in result.exception.message.lower()
    assert len(result.exception.traceback) > 0

    # Verify namespace is intact
    assert "x" in result.namespace_vars
    assert "y" not in result.namespace_vars, "Failed assignment shouldn't add variable"

    print("  ✓ Runtime errors captured correctly")


def test_syntax_error():
    """Test that syntax errors are caught."""
    print("\nTest 2: Syntax error handling...")

    engine = REPLEngine()

    result = engine.execute("def foo(")

    assert not result.success
    assert result.exception is not None
    assert result.exception.type == "SyntaxError"
    print("  ✓ Syntax errors caught correctly")


def test_recovery_after_error():
    """Test that REPL recovers after errors."""
    print("\nTest 3: Recovery after error...")

    engine = REPLEngine()

    # Execute successful code
    result1 = engine.execute("x = 10")
    assert result1.success

    # Execute failing code
    result2 = engine.execute("y = x / 0")
    assert not result2.success

    # Execute successful code again
    result3 = engine.execute("z = x + 5")
    assert result3.success
    assert result3.namespace_vars["z"] == "15"

    print("  ✓ REPL recovers after errors")


def test_name_error():
    """Test undefined variable access."""
    print("\nTest 4: NameError handling...")

    engine = REPLEngine()

    result = engine.execute("print(undefined_var)")

    assert not result.success
    assert result.exception is not None
    assert result.exception.type == "NameError"
    print("  ✓ NameError handled correctly")


def test_multiple_errors():
    """Test handling multiple errors in sequence."""
    print("\nTest 5: Multiple errors...")

    engine = REPLEngine()

    errors = [
        ("1 / 0", "ZeroDivisionError"),
        ("int('not a number')", "ValueError"),
        ("[1, 2, 3][10]", "IndexError"),
        ("{'a': 1}['b']", "KeyError"),
    ]

    for code, expected_type in errors:
        result = engine.execute(code)
        assert not result.success
        assert result.exception.type == expected_type

    # Verify REPL still works
    result = engine.execute("x = 42")
    assert result.success

    print("  ✓ Multiple errors handled correctly")


if __name__ == "__main__":
    print("=" * 60)
    print("Example 02: Error Handling")
    print("=" * 60)

    try:
        test_runtime_error()
        test_syntax_error()
        test_recovery_after_error()
        test_name_error()
        test_multiple_errors()

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
