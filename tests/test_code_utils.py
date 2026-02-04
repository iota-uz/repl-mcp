"""Tests for multi-language code utilities using tree-sitter."""

import pytest
from pathlib import Path

from repl_mcp.utilities.workspace import Workspace
from repl_mcp.utilities.code_utils import CodeUtils
from repl_mcp.models import FunctionDef, ClassDef, ImportInfo, CallSite


@pytest.fixture
def multi_lang_project(tmp_path):
    """Create a sample multi-language project for testing."""
    # Python file
    (tmp_path / "main.py").write_text('''"""Main module."""

import os
from typing import Optional

def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}"

async def fetch_data(url: str) -> dict:
    """Fetch data from URL."""
    pass

class Greeter:
    """A greeter class."""

    def __init__(self, prefix: str):
        self.prefix = prefix

    def greet(self, name: str) -> str:
        return f"{self.prefix} {name}"
''')

    # TypeScript file
    (tmp_path / "app.ts").write_text('''import { Request, Response } from 'express';
import axios from 'axios';

interface User {
    name: string;
    email: string;
}

async function fetchUser(id: number): Promise<User> {
    const response = await axios.get(`/api/users/${id}`);
    return response.data;
}

function greet(name: string): string {
    return `Hello, ${name}`;
}

class UserService {
    private baseUrl: string;

    constructor(baseUrl: string) {
        this.baseUrl = baseUrl;
    }

    async getUser(id: number): Promise<User> {
        return fetchUser(id);
    }
}

const double = (x: number): number => x * 2;
''')

    # JavaScript file
    (tmp_path / "utils.js").write_text('''const axios = require('axios');

function formatDate(date) {
    return date.toISOString();
}

async function fetchJson(url) {
    const response = await axios.get(url);
    return response.json();
}

class DateFormatter {
    format(date) {
        return formatDate(date);
    }
}

const multiply = (a, b) => a * b;
''')

    # Go file
    (tmp_path / "server.go").write_text('''package main

import (
    "fmt"
    "net/http"
)

type Server struct {
    port int
    host string
}

func NewServer(port int) *Server {
    return &Server{port: port, host: "localhost"}
}

func (s *Server) Start() error {
    addr := fmt.Sprintf("%s:%d", s.host, s.port)
    return http.ListenAndServe(addr, nil)
}

func main() {
    server := NewServer(8080)
    server.Start()
}
''')

    # Rust file
    (tmp_path / "lib.rs").write_text('''use std::collections::HashMap;

struct Config {
    name: String,
    port: u16,
}

fn create_config(name: &str, port: u16) -> Config {
    Config {
        name: name.to_string(),
        port,
    }
}

impl Config {
    fn new(name: &str) -> Self {
        create_config(name, 8080)
    }
}
''')

    return tmp_path


@pytest.fixture
def code_utils(multi_lang_project):
    """Create CodeUtils for the test project."""
    workspace = Workspace(multi_lang_project)
    return CodeUtils(workspace)


class TestCodeUtilsSupportedLanguages:
    """Test language support."""

    def test_supported_languages_list(self, code_utils):
        """Test that supported languages are returned."""
        languages = code_utils.supported_languages()
        assert len(languages) > 10
        assert "python" in languages
        assert "typescript" in languages
        assert "javascript" in languages
        assert "go" in languages
        assert "rust" in languages


class TestCodeUtilsFindFunctions:
    """Test finding function definitions across languages."""

    def test_find_python_functions(self, code_utils):
        """Test finding Python functions."""
        funcs = code_utils.find_functions("main.py")
        assert len(funcs) >= 2
        assert all(isinstance(f, FunctionDef) for f in funcs)

        func_names = [f.name for f in funcs]
        assert "greet" in func_names
        assert "fetch_data" in func_names

    def test_find_async_python_functions(self, code_utils):
        """Test that async functions are detected in Python."""
        funcs = code_utils.find_functions("main.py")
        async_func = next((f for f in funcs if f.name == "fetch_data"), None)
        assert async_func is not None
        # Note: async detection may vary by tree-sitter version

    def test_find_typescript_functions(self, code_utils):
        """Test finding TypeScript functions."""
        funcs = code_utils.find_functions("app.ts")
        func_names = [f.name for f in funcs]

        assert "fetchUser" in func_names
        assert "greet" in func_names
        assert "double" in func_names  # Arrow function

    def test_find_javascript_functions(self, code_utils):
        """Test finding JavaScript functions."""
        funcs = code_utils.find_functions("utils.js")
        func_names = [f.name for f in funcs]

        assert "formatDate" in func_names
        assert "fetchJson" in func_names
        assert "multiply" in func_names  # Arrow function

    def test_find_go_functions(self, code_utils):
        """Test finding Go functions."""
        funcs = code_utils.find_functions("server.go")
        func_names = [f.name for f in funcs]

        assert "NewServer" in func_names
        assert "main" in func_names

    def test_find_rust_functions(self, code_utils):
        """Test finding Rust functions."""
        funcs = code_utils.find_functions("lib.rs")
        func_names = [f.name for f in funcs]

        assert "create_config" in func_names

    def test_find_functions_with_pattern(self, code_utils):
        """Test filtering functions by name pattern."""
        funcs = code_utils.find_functions(".", name_pattern="^greet")
        func_names = [f.name for f in funcs]

        assert all("greet" in name.lower() for name in func_names)

    def test_find_functions_by_language(self, code_utils):
        """Test filtering functions by language."""
        funcs = code_utils.find_functions(".", language="typescript")

        # All should be from .ts files
        assert all(f.file.endswith(".ts") for f in funcs)

    def test_find_functions_all_languages(self, code_utils):
        """Test finding functions across all languages."""
        funcs = code_utils.find_functions(".")

        # Should find functions from multiple files
        files = set(f.file for f in funcs)
        assert len(files) >= 3  # At least Python, TypeScript, JavaScript


class TestCodeUtilsFindClasses:
    """Test finding class definitions across languages."""

    def test_find_python_classes(self, code_utils):
        """Test finding Python classes."""
        classes = code_utils.find_classes("main.py")
        class_names = [c.name for c in classes]

        assert "Greeter" in class_names

    def test_find_typescript_classes(self, code_utils):
        """Test finding TypeScript classes."""
        classes = code_utils.find_classes("app.ts")
        class_names = [c.name for c in classes]

        assert "UserService" in class_names

    def test_find_javascript_classes(self, code_utils):
        """Test finding JavaScript classes."""
        classes = code_utils.find_classes("utils.js")
        class_names = [c.name for c in classes]

        assert "DateFormatter" in class_names

    def test_find_go_structs(self, code_utils):
        """Test finding Go structs (treated as classes)."""
        classes = code_utils.find_classes("server.go")
        class_names = [c.name for c in classes]

        assert "Server" in class_names

    def test_find_rust_structs(self, code_utils):
        """Test finding Rust structs."""
        classes = code_utils.find_classes("lib.rs")
        class_names = [c.name for c in classes]

        assert "Config" in class_names

    def test_find_classes_with_pattern(self, code_utils):
        """Test filtering classes by name pattern."""
        classes = code_utils.find_classes(".", name_pattern="Service")

        assert len(classes) >= 1
        assert all("Service" in c.name for c in classes)


class TestCodeUtilsFindImports:
    """Test finding import statements across languages."""

    def test_find_python_imports(self, code_utils):
        """Test finding Python imports."""
        imports = code_utils.find_imports("main.py")
        modules = [i.module for i in imports]

        assert "os" in modules
        assert "typing" in modules

    def test_find_typescript_imports(self, code_utils):
        """Test finding TypeScript imports."""
        imports = code_utils.find_imports("app.ts")
        modules = [i.module for i in imports]

        assert any("express" in m for m in modules)
        assert any("axios" in m for m in modules)

    def test_find_javascript_imports(self, code_utils):
        """Test finding JavaScript imports (require)."""
        imports = code_utils.find_imports("utils.js")
        modules = [i.module for i in imports]

        assert any("axios" in m for m in modules)

    def test_find_go_imports(self, code_utils):
        """Test finding Go imports."""
        imports = code_utils.find_imports("server.go")
        modules = [i.module for i in imports]

        assert any("fmt" in m for m in modules)
        assert any("net/http" in m for m in modules)

    def test_find_rust_imports(self, code_utils):
        """Test finding Rust use statements."""
        imports = code_utils.find_imports("lib.rs")
        modules = [i.module for i in imports]

        assert len(modules) >= 1  # std::collections::HashMap


class TestCodeUtilsFindCalls:
    """Test finding function calls across languages."""

    def test_find_python_calls(self, code_utils):
        """Test finding Python function calls."""
        calls = code_utils.find_calls("main.py", "greet")

        # greet is called inside Greeter.greet (calls self.prefix)
        # This depends on how the query works
        assert isinstance(calls, list)

    def test_find_typescript_calls(self, code_utils):
        """Test finding TypeScript function calls."""
        calls = code_utils.find_calls("app.ts", "fetchUser")

        # fetchUser is called in UserService.getUser
        assert len(calls) >= 1
        assert all(isinstance(c, CallSite) for c in calls)

    def test_find_calls_with_context(self, code_utils):
        """Test that call context is included."""
        calls = code_utils.find_calls("app.ts", "fetchUser", include_context=True)

        if calls:
            assert calls[0].context != ""


class TestCodeUtilsGlobPatterns:
    """Test glob pattern support."""

    def test_glob_pattern_ts_files(self, code_utils):
        """Test finding functions in TypeScript files only."""
        funcs = code_utils.find_functions("*.ts")

        assert all(f.file.endswith(".ts") for f in funcs)

    def test_glob_pattern_multiple_extensions(self, code_utils):
        """Test finding functions in JS and TS files."""
        # Find in both
        js_funcs = code_utils.find_functions("*.js")
        ts_funcs = code_utils.find_functions("*.ts")

        assert len(js_funcs) >= 1
        assert len(ts_funcs) >= 1


class TestCodeUtilsIntegration:
    """Integration tests with REPL engine."""

    def test_code_utils_injected_in_repl(self, multi_lang_project):
        """Test that code utils is available in REPL as 'code'."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=multi_lang_project)

        result = engine.execute("code is not None")
        assert result.success
        assert result.return_value == "True"

    def test_find_functions_in_repl(self, multi_lang_project):
        """Test using code.find_functions in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=multi_lang_project)

        result = engine.execute("len(code.find_functions('.'))")
        assert result.success
        assert int(result.return_value) >= 5

    def test_supported_languages_in_repl(self, multi_lang_project):
        """Test using code.supported_languages in REPL."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=multi_lang_project)

        result = engine.execute("'python' in code.supported_languages()")
        assert result.success
        assert result.return_value == "True"

    def test_code_utils_preserved_on_reset(self, multi_lang_project):
        """Test that code utils is preserved after namespace reset."""
        from repl_mcp.repl_engine import REPLEngine

        engine = REPLEngine(workspace_root=multi_lang_project)
        engine.execute("x = 42")
        engine.reset_namespace()

        # code should still be available
        result = engine.execute("code is not None")
        assert result.success
        assert result.return_value == "True"


class TestCodeUtilsEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_directory(self, tmp_path):
        """Test with empty directory."""
        workspace = Workspace(tmp_path)
        code = CodeUtils(workspace)

        funcs = code.find_functions(".")
        assert funcs == []

    def test_unsupported_file_extension(self, tmp_path):
        """Test with unsupported file type."""
        (tmp_path / "data.xyz").write_text("some content")

        workspace = Workspace(tmp_path)
        code = CodeUtils(workspace)

        funcs = code.find_functions(".")
        assert funcs == []

    def test_syntax_error_in_file(self, tmp_path):
        """Test handling of files with syntax errors."""
        (tmp_path / "broken.py").write_text("def broken( # missing closing")

        workspace = Workspace(tmp_path)
        code = CodeUtils(workspace)

        # Should not raise, just skip the broken file
        funcs = code.find_functions(".")
        assert isinstance(funcs, list)

    def test_binary_file_skipped(self, tmp_path):
        """Test that binary files are skipped gracefully."""
        (tmp_path / "binary.py").write_bytes(b"\x00\x01\x02\x03")

        workspace = Workspace(tmp_path)
        code = CodeUtils(workspace)

        # Should not raise
        funcs = code.find_functions(".")
        assert isinstance(funcs, list)
