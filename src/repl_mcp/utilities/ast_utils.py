"""AST utilities for Python code analysis."""

import ast
import re
from pathlib import Path
from typing import Optional, Union

from .base import REPLUtility, ASTError, ParseError
from .workspace import Workspace
from ..models import (
    CallSite,
    FunctionDef,
    ClassDef,
    ImportInfo,
    DependencyGraph,
    UsageInfo,
    ComplexityMetrics,
)


class ASTUtils(REPLUtility):
    """AST-based code analysis utilities for Python.

    Example usage in REPL:
        ast_utils.find_function_calls("src/", "deprecated_api")
        ast_utils.find_function_definitions("src/")
        ast_utils.find_imports("src/main.py")
        ast_utils.dependency_graph("src/")
    """

    def __init__(self, workspace: Workspace):
        """Initialize AST utilities.

        Args:
            workspace: Workspace for file access
        """
        self.workspace = workspace

    def _parse_file(self, path: str) -> Optional[ast.Module]:
        """Parse a Python file into an AST.

        Args:
            path: Relative path to file

        Returns:
            AST Module or None if parsing fails
        """
        try:
            content = self.workspace.read(path)
            return ast.parse(content, filename=path)
        except (SyntaxError, UnicodeDecodeError):
            return None

    def _get_python_files(self, path: str, recursive: bool = True) -> list[str]:
        """Get all Python files in a path.

        Args:
            path: File or directory path
            recursive: Search recursively

        Returns:
            List of Python file paths
        """
        if self.workspace.is_file(path):
            return [path] if path.endswith(".py") else []

        pattern = "**/*.py" if recursive else "*.py"
        if path != ".":
            pattern = f"{path}/{pattern}"

        return self.workspace.glob(pattern)

    def _get_full_name(self, node: ast.AST) -> str:
        """Get the full dotted name from an AST node.

        Args:
            node: AST node (Name, Attribute, or Call)

        Returns:
            Full dotted name string
        """
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            value = self._get_full_name(node.value)
            return f"{value}.{node.attr}" if value else node.attr
        elif isinstance(node, ast.Call):
            return self._get_full_name(node.func)
        return ""

    def _get_decorator_name(self, node: ast.AST) -> str:
        """Get decorator name from a decorator node."""
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return self._get_full_name(node)
        elif isinstance(node, ast.Call):
            return self._get_full_name(node.func)
        return ""

    def find_function_calls(
        self,
        path: str,
        function_name: str,
        recursive: bool = True,
        include_context: bool = True,
    ) -> list[CallSite]:
        """Find all calls to a specific function.

        Args:
            path: File or directory to search
            function_name: Function name to find (can be partial, e.g., "deprecated" matches "deprecated_api")
            recursive: Search subdirectories
            include_context: Include surrounding code context

        Returns:
            List of CallSite objects
        """
        results = []
        files = self._get_python_files(path, recursive)

        for file_path in files:
            tree = self._parse_file(file_path)
            if tree is None:
                continue

            # Get file content for context
            try:
                lines = self.workspace.read_lines(file_path) if include_context else []
            except Exception:
                lines = []

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    full_name = self._get_full_name(node.func)
                    # Match if function_name appears anywhere in the full name
                    if function_name in full_name:
                        context = ""
                        if include_context and lines and hasattr(node, "lineno"):
                            line_idx = node.lineno - 1
                            if 0 <= line_idx < len(lines):
                                context = lines[line_idx].strip()

                        call_site = CallSite(
                            file=file_path,
                            line=node.lineno,
                            column=node.col_offset,
                            function_name=full_name.split(".")[-1],
                            full_call=full_name,
                            context=context,
                        )
                        results.append(call_site)

        return results

    def find_function_definitions(
        self,
        path: str,
        name_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[FunctionDef]:
        """Find function definitions.

        Args:
            path: File or directory to search
            name_pattern: Regex pattern to filter function names (optional)
            recursive: Search subdirectories

        Returns:
            List of FunctionDef objects
        """
        results = []
        files = self._get_python_files(path, recursive)
        pattern = re.compile(name_pattern) if name_pattern else None

        for file_path in files:
            tree = self._parse_file(file_path)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Filter by name pattern
                    if pattern and not pattern.search(node.name):
                        continue

                    # Get parameters
                    params = []
                    for arg in node.args.args:
                        params.append(arg.arg)
                    for arg in node.args.posonlyargs:
                        params.append(arg.arg)
                    for arg in node.args.kwonlyargs:
                        params.append(arg.arg)
                    if node.args.vararg:
                        params.append(f"*{node.args.vararg.arg}")
                    if node.args.kwarg:
                        params.append(f"**{node.args.kwarg.arg}")

                    # Get return annotation
                    return_annotation = None
                    if node.returns:
                        return_annotation = ast.unparse(node.returns)

                    # Get docstring
                    docstring = ast.get_docstring(node)

                    # Get decorators
                    decorators = [self._get_decorator_name(d) for d in node.decorator_list]

                    # Check if it's a method (inside a class)
                    is_method = any(
                        isinstance(parent, ast.ClassDef)
                        for parent in ast.walk(tree)
                        if hasattr(parent, "body") and node in getattr(parent, "body", [])
                    )

                    func_def = FunctionDef(
                        file=file_path,
                        line=node.lineno,
                        end_line=node.end_lineno,
                        name=node.name,
                        params=params,
                        return_annotation=return_annotation,
                        docstring=docstring[:200] if docstring else None,
                        is_async=isinstance(node, ast.AsyncFunctionDef),
                        is_method=is_method,
                        decorators=decorators,
                    )
                    results.append(func_def)

        return results

    def find_class_definitions(
        self,
        path: str,
        name_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[ClassDef]:
        """Find class definitions.

        Args:
            path: File or directory to search
            name_pattern: Regex pattern to filter class names (optional)
            recursive: Search subdirectories

        Returns:
            List of ClassDef objects
        """
        results = []
        files = self._get_python_files(path, recursive)
        pattern = re.compile(name_pattern) if name_pattern else None

        for file_path in files:
            tree = self._parse_file(file_path)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    # Filter by name pattern
                    if pattern and not pattern.search(node.name):
                        continue

                    # Get base classes
                    bases = [self._get_full_name(base) for base in node.bases]

                    # Get methods
                    methods = []
                    class_vars = []
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            methods.append(item.name)
                        elif isinstance(item, ast.Assign):
                            for target in item.targets:
                                if isinstance(target, ast.Name):
                                    class_vars.append(target.id)
                        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                            class_vars.append(item.target.id)

                    # Get docstring
                    docstring = ast.get_docstring(node)

                    # Get decorators
                    decorators = [self._get_decorator_name(d) for d in node.decorator_list]

                    class_def = ClassDef(
                        file=file_path,
                        line=node.lineno,
                        end_line=node.end_lineno,
                        name=node.name,
                        bases=bases,
                        methods=methods,
                        class_variables=class_vars,
                        docstring=docstring[:200] if docstring else None,
                        decorators=decorators,
                    )
                    results.append(class_def)

        return results

    def find_imports(
        self,
        path: str,
        module_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[ImportInfo]:
        """Find import statements.

        Args:
            path: File or directory to search
            module_pattern: Regex pattern to filter module names (optional)
            recursive: Search subdirectories

        Returns:
            List of ImportInfo objects
        """
        results = []
        files = self._get_python_files(path, recursive)
        pattern = re.compile(module_pattern) if module_pattern else None

        for file_path in files:
            tree = self._parse_file(file_path)
            if tree is None:
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        # Filter by module pattern
                        if pattern and not pattern.search(alias.name):
                            continue

                        import_info = ImportInfo(
                            file=file_path,
                            line=node.lineno,
                            module=alias.name,
                            names=[],
                            alias=alias.asname,
                            is_from_import=False,
                            is_relative=False,
                        )
                        results.append(import_info)

                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    # Filter by module pattern
                    if pattern and not pattern.search(module):
                        continue

                    names = [alias.name for alias in node.names]
                    aliases = [alias.asname for alias in node.names if alias.asname]

                    import_info = ImportInfo(
                        file=file_path,
                        line=node.lineno,
                        module=module,
                        names=names,
                        alias=aliases[0] if len(aliases) == 1 else None,
                        is_from_import=True,
                        is_relative=node.level > 0,
                    )
                    results.append(import_info)

        return results

    def get_exports(self, path: str) -> list[str]:
        """Get exported names from a Python file.

        This returns names in __all__ if defined, otherwise all public names
        (names not starting with underscore).

        Args:
            path: Path to Python file

        Returns:
            List of exported names
        """
        tree = self._parse_file(path)
        if tree is None:
            return []

        # Look for __all__
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "__all__":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            return [
                                elt.value
                                for elt in node.value.elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            ]

        # No __all__, return public names
        exports = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if not node.name.startswith("_"):
                    exports.append(node.name)
            elif isinstance(node, ast.AsyncFunctionDef):
                if not node.name.startswith("_"):
                    exports.append(node.name)
            elif isinstance(node, ast.ClassDef):
                if not node.name.startswith("_"):
                    exports.append(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and not target.id.startswith("_"):
                        exports.append(target.id)

        return exports

    def dependency_graph(
        self,
        path: str,
        recursive: bool = True,
    ) -> DependencyGraph:
        """Build a dependency graph for Python files.

        Args:
            path: Directory to analyze
            recursive: Include subdirectories

        Returns:
            DependencyGraph with nodes, edges, and external dependencies
        """
        files = self._get_python_files(path, recursive)
        nodes = set(files)
        edges = []
        external_deps = set()

        # Build mapping of module names to file paths
        module_to_file = {}
        for file_path in files:
            # Convert file path to module name
            module_name = file_path.replace("/", ".").replace("\\", ".")
            if module_name.endswith(".py"):
                module_name = module_name[:-3]
            module_to_file[module_name] = file_path

            # Also add the file's parent package
            parts = module_name.split(".")
            for i in range(len(parts)):
                partial = ".".join(parts[: i + 1])
                if partial not in module_to_file:
                    module_to_file[partial] = file_path

        for file_path in files:
            imports = self.find_imports(file_path, recursive=False)

            for imp in imports:
                module = imp.module
                if not module:
                    continue

                # Try to find the module in our codebase
                found = False
                for mod_name, mod_file in module_to_file.items():
                    if module == mod_name or module.startswith(mod_name + "."):
                        if mod_file != file_path:
                            edges.append((file_path, mod_file))
                        found = True
                        break

                if not found:
                    # External dependency
                    top_level = module.split(".")[0]
                    external_deps.add(top_level)

        return DependencyGraph(
            nodes=sorted(nodes),
            edges=list(set(edges)),
            external_deps=sorted(external_deps),
        )

    def find_usages(
        self,
        path: str,
        name: str,
        recursive: bool = True,
    ) -> list[UsageInfo]:
        """Find all usages of a name (variable, function, class).

        Args:
            path: File or directory to search
            name: Name to find
            recursive: Search subdirectories

        Returns:
            List of UsageInfo objects
        """
        results = []
        files = self._get_python_files(path, recursive)

        for file_path in files:
            tree = self._parse_file(file_path)
            if tree is None:
                continue

            try:
                lines = self.workspace.read_lines(file_path)
            except Exception:
                lines = []

            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id == name:
                    context = ""
                    if lines and hasattr(node, "lineno"):
                        line_idx = node.lineno - 1
                        if 0 <= line_idx < len(lines):
                            context = lines[line_idx].strip()

                    results.append(UsageInfo(
                        file=file_path,
                        line=node.lineno,
                        column=node.col_offset,
                        context=context,
                    ))

        return results

    def complexity(self, path: str) -> ComplexityMetrics:
        """Calculate basic complexity metrics for a file.

        Args:
            path: Path to Python file

        Returns:
            ComplexityMetrics model instance

        Raises:
            ParseError: If file cannot be parsed
        """
        tree = self._parse_file(path)
        if tree is None:
            raise ParseError(path, "Failed to parse file")

        num_functions = 0
        num_classes = 0
        num_imports = 0
        total_lines = 0
        max_depth = 0

        def calc_depth(node: ast.AST, current_depth: int = 0) -> int:
            """Calculate maximum nesting depth."""
            max_d = current_depth
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                    max_d = max(max_d, calc_depth(child, current_depth + 1))
                else:
                    max_d = max(max_d, calc_depth(child, current_depth))
            return max_d

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                num_functions += 1
            elif isinstance(node, ast.ClassDef):
                num_classes += 1
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                num_imports += 1

        max_depth = calc_depth(tree)

        try:
            total_lines = len(self.workspace.read_lines(path))
        except Exception:
            pass

        return ComplexityMetrics(
            file=path,
            lines=total_lines,
            functions=num_functions,
            classes=num_classes,
            imports=num_imports,
            max_nesting_depth=max_depth,
        )

    def __repr__(self) -> str:
        return f"ASTUtils(workspace={self.workspace.root})"
