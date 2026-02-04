"""Tests for AST utilities."""

import pytest
from pathlib import Path

from repl_mcp.utilities.workspace import Workspace
from repl_mcp.utilities.ast_utils import ASTUtils
from repl_mcp.models import CallSite, FunctionDef, ClassDef, ImportInfo, DependencyGraph


@pytest.fixture
def python_project(tmp_path):
    """Create a sample Python project for testing."""
    # Create directory structure
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "utils").mkdir()

    # Main module
    (tmp_path / "src" / "main.py").write_text('''"""Main module."""

import os
from typing import Optional
from .utils.helpers import deprecated_api, helper_func

def main():
    """Main entry point."""
    result = deprecated_api("test")
    helper_func(result)
    return result

async def async_main():
    """Async main entry point."""
    return await some_async_call()

class Application:
    """Main application class."""

    name: str
    version: str = "1.0.0"

    def __init__(self, name: str):
        self.name = name

    def run(self):
        deprecated_api("running")

    @staticmethod
    def create() -> "Application":
        return Application("default")

if __name__ == "__main__":
    main()
''')

    # Utils package
    (tmp_path / "src" / "utils" / "__init__.py").write_text('''"""Utils package."""

from .helpers import helper_func, deprecated_api

__all__ = ["helper_func", "deprecated_api"]
''')

    # Helpers module
    (tmp_path / "src" / "utils" / "helpers.py").write_text('''"""Helper functions."""

import re
from typing import Any

def deprecated_api(arg: str) -> str:
    """Deprecated function, do not use."""
    return f"deprecated: {arg}"

def helper_func(value: Any) -> None:
    """Helper function."""
    print(value)

def _private_func():
    """Private helper."""
    pass

class HelperClass:
    """A helper class."""

    def method(self):
        pass
''')

    # Test file
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text('''"""Tests for main module."""

import pytest
from src.main import main, Application

def test_main():
    result = main()
    assert result is not None

def test_application():
    app = Application("test")
    assert app.name == "test"
''')

    return tmp_path


@pytest.fixture
def ast_utils(python_project):
    """Create ASTUtils for the test project."""
    workspace = Workspace(python_project)
    return ASTUtils(workspace)


class TestASTUtilsFindFunctionCalls:
    """Test finding function calls."""

    def test_find_function_calls_basic(self, ast_utils):
        """Test finding calls to a function."""
        calls = ast_utils.find_function_calls("src/", "deprecated_api")
        assert len(calls) >= 2  # Called in main.py and Application.run
        assert all(isinstance(c, CallSite) for c in calls)

    def test_find_function_calls_info(self, ast_utils):
        """Test call site information."""
        calls = ast_utils.find_function_calls("src/main.py", "deprecated_api")
        assert len(calls) >= 1

        call = calls[0]
        assert call.file == "src/main.py"
        assert call.line > 0
        assert "deprecated_api" in call.full_call

    def test_find_function_calls_with_context(self, ast_utils):
        """Test that context is included."""
        calls = ast_utils.find_function_calls("src/main.py", "deprecated_api", include_context=True)
        assert len(calls) >= 1
        assert calls[0].context != ""

    def test_find_function_calls_partial_match(self, ast_utils):
        """Test partial function name matching."""
        calls = ast_utils.find_function_calls("src/", "deprecated")
        assert len(calls) >= 2

    def test_find_function_calls_no_match(self, ast_utils):
        """Test when no calls match."""
        calls = ast_utils.find_function_calls("src/", "nonexistent_function")
        assert len(calls) == 0


class TestASTUtilsFindFunctionDefinitions:
    """Test finding function definitions."""

    def test_find_function_definitions_basic(self, ast_utils):
        """Test finding function definitions."""
        funcs = ast_utils.find_function_definitions("src/")
        assert len(funcs) >= 4  # main, async_main, deprecated_api, helper_func, etc.
        assert all(isinstance(f, FunctionDef) for f in funcs)

    def test_find_function_definitions_info(self, ast_utils):
        """Test function definition information."""
        funcs = ast_utils.find_function_definitions("src/utils/helpers.py")
        func_names = [f.name for f in funcs]

        assert "deprecated_api" in func_names
        assert "helper_func" in func_names
        assert "_private_func" in func_names

    def test_find_function_definitions_with_pattern(self, ast_utils):
        """Test filtering by name pattern."""
        funcs = ast_utils.find_function_definitions("src/", name_pattern="^main")
        func_names = [f.name for f in funcs]
        assert "main" in func_names

    def test_find_async_functions(self, ast_utils):
        """Test finding async functions."""
        funcs = ast_utils.find_function_definitions("src/main.py")
        async_funcs = [f for f in funcs if f.is_async]
        assert len(async_funcs) >= 1
        assert async_funcs[0].name == "async_main"

    def test_find_function_with_decorators(self, ast_utils):
        """Test finding functions with decorators."""
        funcs = ast_utils.find_function_definitions("src/main.py")
        decorated = [f for f in funcs if f.decorators]
        assert len(decorated) >= 1
        assert "staticmethod" in decorated[0].decorators

    def test_find_function_parameters(self, ast_utils):
        """Test function parameter extraction."""
        funcs = ast_utils.find_function_definitions("src/utils/helpers.py", name_pattern="deprecated_api")
        assert len(funcs) == 1
        assert "arg" in funcs[0].params

    def test_find_function_return_annotation(self, ast_utils):
        """Test return type annotation extraction."""
        funcs = ast_utils.find_function_definitions("src/utils/helpers.py", name_pattern="deprecated_api")
        assert len(funcs) == 1
        assert funcs[0].return_annotation == "str"


class TestASTUtilsFindClassDefinitions:
    """Test finding class definitions."""

    def test_find_class_definitions_basic(self, ast_utils):
        """Test finding class definitions."""
        classes = ast_utils.find_class_definitions("src/")
        assert len(classes) >= 2  # Application, HelperClass
        assert all(isinstance(c, ClassDef) for c in classes)

    def test_find_class_definitions_info(self, ast_utils):
        """Test class definition information."""
        classes = ast_utils.find_class_definitions("src/main.py")
        assert len(classes) >= 1

        app_class = next(c for c in classes if c.name == "Application")
        assert "run" in app_class.methods
        assert "create" in app_class.methods
        assert "__init__" in app_class.methods

    def test_find_class_with_pattern(self, ast_utils):
        """Test filtering by name pattern."""
        classes = ast_utils.find_class_definitions("src/", name_pattern="Helper")
        assert len(classes) >= 1
        assert all("Helper" in c.name for c in classes)

    def test_find_class_variables(self, ast_utils):
        """Test class variable extraction."""
        classes = ast_utils.find_class_definitions("src/main.py", name_pattern="Application")
        assert len(classes) == 1
        assert "name" in classes[0].class_variables
        assert "version" in classes[0].class_variables


class TestASTUtilsFindImports:
    """Test finding imports."""

    def test_find_imports_basic(self, ast_utils):
        """Test finding import statements."""
        imports = ast_utils.find_imports("src/")
        assert len(imports) >= 3  # os, typing, re, etc.
        assert all(isinstance(i, ImportInfo) for i in imports)

    def test_find_imports_regular(self, ast_utils):
        """Test regular import statements."""
        imports = ast_utils.find_imports("src/main.py")
        os_import = next((i for i in imports if i.module == "os"), None)
        assert os_import is not None
        assert os_import.is_from_import is False

    def test_find_imports_from(self, ast_utils):
        """Test from import statements."""
        imports = ast_utils.find_imports("src/main.py")
        typing_import = next((i for i in imports if i.module == "typing"), None)
        assert typing_import is not None
        assert typing_import.is_from_import is True
        assert "Optional" in typing_import.names

    def test_find_imports_with_pattern(self, ast_utils):
        """Test filtering by module pattern."""
        imports = ast_utils.find_imports("src/", module_pattern="typing")
        assert len(imports) >= 1
        assert all("typing" in i.module for i in imports)

    def test_find_relative_imports(self, ast_utils):
        """Test finding relative imports."""
        imports = ast_utils.find_imports("src/main.py")
        relative = [i for i in imports if i.is_relative]
        assert len(relative) >= 1


class TestASTUtilsGetExports:
    """Test getting exports."""

    def test_get_exports_with_all(self, ast_utils):
        """Test getting exports when __all__ is defined."""
        exports = ast_utils.get_exports("src/utils/__init__.py")
        assert "helper_func" in exports
        assert "deprecated_api" in exports

    def test_get_exports_without_all(self, ast_utils):
        """Test getting exports without __all__."""
        exports = ast_utils.get_exports("src/utils/helpers.py")
        assert "deprecated_api" in exports
        assert "helper_func" in exports
        assert "HelperClass" in exports
        # Private should not be exported
        assert "_private_func" not in exports


class TestASTUtilsDependencyGraph:
    """Test dependency graph building."""

    def test_dependency_graph_basic(self, ast_utils):
        """Test building dependency graph."""
        graph = ast_utils.dependency_graph("src/")
        assert isinstance(graph, DependencyGraph)
        assert len(graph.nodes) >= 3

    def test_dependency_graph_external(self, ast_utils):
        """Test that external deps are identified."""
        graph = ast_utils.dependency_graph("src/")
        # os, typing, re are external
        assert "os" in graph.external_deps or "typing" in graph.external_deps


class TestASTUtilsFindUsages:
    """Test finding name usages."""

    def test_find_usages_basic(self, ast_utils):
        """Test finding usages of a name."""
        usages = ast_utils.find_usages("src/main.py", "result")
        assert len(usages) >= 2  # assigned and used


class TestASTUtilsComplexity:
    """Test complexity analysis."""

    def test_complexity_basic(self, ast_utils):
        """Test basic complexity metrics."""
        from repl_mcp.models import ComplexityMetrics

        metrics = ast_utils.complexity("src/main.py")
        assert isinstance(metrics, ComplexityMetrics)
        assert metrics.functions >= 2
        assert metrics.classes >= 1
        assert metrics.imports >= 1
        assert metrics.lines > 0


class TestASTUtilsIntegration:
    """Integration tests for AST utilities with REPL engine."""

    def test_ast_utils_injected_in_repl(self, python_project):
        """Test that ast_utils is available in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=python_project)

        result = engine.execute("ast_utils is not None")
        assert result.success
        assert result.return_value == "True"

    def test_find_functions_in_repl(self, python_project):
        """Test using ast_utils.find_function_definitions in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=python_project)

        result = engine.execute("len(ast_utils.find_function_definitions('src/'))")
        assert result.success
        # Should find multiple functions
        assert int(result.return_value) >= 4

    def test_find_calls_in_repl(self, python_project):
        """Test using ast_utils.find_function_calls in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=python_project)

        result = engine.execute("len(ast_utils.find_function_calls('src/', 'deprecated'))")
        assert result.success
        assert int(result.return_value) >= 1

    def test_ast_utils_preserved_on_reset(self, python_project):
        """Test that ast_utils is preserved after namespace reset."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=python_project)
        engine.execute("x = 42")
        engine.reset_namespace()

        # ast_utils should still be available
        result = engine.execute("ast_utils is not None")
        assert result.success
        assert result.return_value == "True"
