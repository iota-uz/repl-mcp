#!/bin/bash
# Run all example scripts sequentially

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "======================================================================"
echo "Running all REPL MCP examples"
echo "======================================================================"
echo

# Track results
PASSED=0
FAILED=0
FAILED_TESTS=()

# Find all example Python files
for example in "$SCRIPT_DIR"/*.py; do
    if [ -f "$example" ]; then
        filename=$(basename "$example")
        echo
        echo "----------------------------------------------------------------------"
        echo "Running: $filename"
        echo "----------------------------------------------------------------------"

        if cd "$PROJECT_DIR" && uv run python "$example"; then
            PASSED=$((PASSED + 1))
        else
            FAILED=$((FAILED + 1))
            FAILED_TESTS+=("$filename")
            echo "✗ $filename FAILED"
        fi
    fi
done

echo
echo "======================================================================"
echo "Results Summary"
echo "======================================================================"
echo "Passed: $PASSED"
echo "Failed: $FAILED"

if [ $FAILED -gt 0 ]; then
    echo
    echo "Failed tests:"
    for test in "${FAILED_TESTS[@]}"; do
        echo "  - $test"
    done
    exit 1
else
    echo
    echo "✓ All examples passed!"
fi
