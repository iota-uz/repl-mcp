"""Pytest configuration and fixtures."""

import pytest
from repl_mcp.repl_engine import REPLEngine
from repl_mcp.mcp_client_wrapper import MCPClientWrapper


@pytest.fixture
def repl_engine():
    """Create a fresh REPL engine for each test."""
    engine = REPLEngine()
    yield engine
    # Cleanup
    engine.reset_namespace()


@pytest.fixture
def repl_engine_with_mcp():
    """Create a REPL engine with MCP wrapper."""
    mcp_wrapper = MCPClientWrapper()
    engine = REPLEngine(mcp_wrapper=mcp_wrapper)
    yield engine, mcp_wrapper
    # Cleanup
    mcp_wrapper.disconnect()
    engine.reset_namespace()


@pytest.fixture
def mcp_wrapper():
    """Create a fresh MCP client wrapper."""
    wrapper = MCPClientWrapper()
    yield wrapper
    # Cleanup
    wrapper.disconnect()
