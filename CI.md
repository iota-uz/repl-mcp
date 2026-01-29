# CI/CD Setup

## Testing

### Local Testing

```bash
# Run all tests
make test

# Run specific test suites
make test-unit           # Unit tests only
make test-integration    # Integration tests
make test-http          # HTTP server tests
make test-examples      # Example scripts

# Full verification
make verify             # Tests + examples
```

### Test Structure

```
tests/
├── conftest.py              # Pytest fixtures
├── test_models.py           # Data model tests (8 tests)
├── test_repl_engine.py      # REPL engine tests (16 tests)
├── test_server_init.py      # Server initialization (6 tests)
├── test_integration.py      # Integration tests (13 tests)
└── test_http_integration.py # HTTP server tests (2 tests)
```

Total: **45 pytest tests** + **14 example tests** = **59 tests**

## GitHub Actions

The project includes a GitHub Actions workflow (`.github/workflows/test.yml`) that:

- Runs on push/PR to main/develop branches
- Tests on multiple OS (Ubuntu, macOS)
- Tests on multiple Python versions (3.10, 3.11, 3.12)
- Runs all test suites
- Runs example scripts separately

### Workflow Jobs

1. **test** - Main test suite
   - Unit tests
   - Integration tests
   - HTTP server tests
   - CLI verification

2. **example-scripts** - Example validation
   - Runs all example scripts
   - Verifies real-world usage patterns

## Adding New Tests

### Unit Tests

Add to appropriate `test_*.py` file in `tests/`:

```python
def test_my_feature():
    """Test description."""
    # Test code
    assert result == expected
```

### Integration Tests

Add to `tests/test_integration.py`:

```python
class TestMyFeature:
    def test_feature_works(self):
        repl_mcp_server.initialize_server(autoconnect=False)
        # Test code
```

### Example Scripts

Add to `examples/`:

```python
#!/usr/bin/env python3
"""Example: Description."""

def test_example():
    # Test code
    assert result == expected

if __name__ == "__main__":
    test_example()
    print("✓ Example passed!")
```

## Continuous Integration

Tests run automatically on:
- Push to main/develop
- Pull requests
- Manual trigger (workflow_dispatch)

All tests must pass before merging.

## Local CI Simulation

```bash
# Simulate CI locally
make clean
make install
make verify
```

This runs the same tests as CI.
