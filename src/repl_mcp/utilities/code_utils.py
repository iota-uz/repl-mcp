"""Multi-language code analysis utilities using tree-sitter."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_parser, get_language

from .base import REPLUtility, ASTError
from .workspace import Workspace
from ..models import (
    FunctionDef,
    ClassDef,
    ImportInfo,
    CallSite,
)


# =============================================================================
# Language Configuration
# =============================================================================

# Map file extensions to tree-sitter language names
EXTENSION_TO_LANGUAGE = {
    # Python
    ".py": "python",
    ".pyi": "python",
    # JavaScript/TypeScript
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    # Go
    ".go": "go",
    # Rust
    ".rs": "rust",
    # C/C++
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    # Java
    ".java": "java",
    # Ruby
    ".rb": "ruby",
    # PHP
    ".php": "php",
    # C#
    ".cs": "c_sharp",
    # Kotlin
    ".kt": "kotlin",
    ".kts": "kotlin",
    # Swift
    ".swift": "swift",
    # Scala
    ".scala": "scala",
    # Shell
    ".sh": "bash",
    ".bash": "bash",
    # YAML/JSON/TOML
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    # HTML/CSS
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    # SQL
    ".sql": "sql",
    # Markdown
    ".md": "markdown",
}

# Tree-sitter queries for finding functions in each language
FUNCTION_QUERIES = {
    "python": """
        (function_definition
            name: (identifier) @name
            parameters: (parameters) @params
            return_type: (type)? @return_type
        ) @func
    """,
    "javascript": """
        [
            (function_declaration
                name: (identifier) @name
                parameters: (formal_parameters) @params
            ) @func
            (lexical_declaration
                (variable_declarator
                    name: (identifier) @name
                    value: (arrow_function
                        parameters: (formal_parameters) @params
                    ) @func
                )
            )
            (variable_declaration
                (variable_declarator
                    name: (identifier) @name
                    value: (arrow_function
                        parameters: (formal_parameters) @params
                    ) @func
                )
            )
        ]
    """,
    "typescript": """
        [
            (function_declaration
                name: (identifier) @name
                parameters: (formal_parameters) @params
                return_type: (type_annotation)? @return_type
            ) @func
            (lexical_declaration
                (variable_declarator
                    name: (identifier) @name
                    value: (arrow_function
                        parameters: (formal_parameters) @params
                        return_type: (type_annotation)? @return_type
                    ) @func
                )
            )
        ]
    """,
    "go": """
        (function_declaration
            name: (identifier) @name
            parameters: (parameter_list) @params
            result: (_)? @return_type
        ) @func
    """,
    "rust": """
        (function_item
            name: (identifier) @name
            parameters: (parameters) @params
            return_type: (_)? @return_type
        ) @func
    """,
    "java": """
        (method_declaration
            name: (identifier) @name
            parameters: (formal_parameters) @params
            type: (_) @return_type
        ) @func
    """,
    "c": """
        (function_definition
            declarator: (function_declarator
                declarator: (identifier) @name
                parameters: (parameter_list) @params
            )
            type: (_) @return_type
        ) @func
    """,
    "cpp": """
        (function_definition
            declarator: (function_declarator
                declarator: (_) @name
                parameters: (parameter_list) @params
            )
            type: (_)? @return_type
        ) @func
    """,
    "ruby": """
        (method
            name: (_) @name
            parameters: (method_parameters)? @params
        ) @func
    """,
}

# Tree-sitter queries for finding classes in each language
CLASS_QUERIES = {
    "python": """
        (class_definition
            name: (identifier) @name
            superclasses: (argument_list)? @bases
        ) @class
    """,
    "javascript": """
        (class_declaration
            name: (identifier) @name
            (class_heritage)? @bases
        ) @class
    """,
    "typescript": """
        (class_declaration
            name: (type_identifier) @name
            (class_heritage)? @bases
        ) @class
    """,
    "java": """
        (class_declaration
            name: (identifier) @name
            superclass: (superclass)? @bases
            interfaces: (super_interfaces)? @interfaces
        ) @class
    """,
    "rust": """
        [
            (struct_item
                name: (type_identifier) @name
            ) @class
            (impl_item
                type: (type_identifier) @name
            ) @class
        ]
    """,
    "go": """
        (type_declaration
            (type_spec
                name: (type_identifier) @name
                type: (struct_type)
            )
        ) @class
    """,
    "cpp": """
        (class_specifier
            name: (type_identifier) @name
            (base_class_clause)? @bases
        ) @class
    """,
    "ruby": """
        (class
            name: (constant) @name
            superclass: (superclass)? @bases
        ) @class
    """,
}

# Tree-sitter queries for finding imports in each language
IMPORT_QUERIES = {
    "python": """
        [
            (import_statement
                name: (dotted_name) @module
            ) @import
            (import_from_statement
                module_name: (dotted_name)? @module
                name: (dotted_name) @names
            ) @import
        ]
    """,
    "javascript": """
        [
            (import_statement
                source: (string) @module
            ) @import
            (call_expression
                function: (identifier) @_require
                arguments: (arguments (string) @module)
                (#eq? @_require "require")
            ) @import
        ]
    """,
    "typescript": """
        (import_statement
            source: (string) @module
        ) @import
    """,
    "go": """
        [
            (import_declaration
                (import_spec
                    path: (interpreted_string_literal) @module
                )
            ) @import
            (import_declaration
                (import_spec_list
                    (import_spec
                        path: (interpreted_string_literal) @module
                    )
                )
            ) @import
        ]
    """,
    "rust": """
        (use_declaration
            argument: (_) @module
        ) @import
    """,
    "java": """
        (import_declaration
            (scoped_identifier) @module
        ) @import
    """,
}

# Tree-sitter queries for finding function calls
CALL_QUERIES = {
    "python": """
        (call
            function: [
                (identifier) @func_name
                (attribute
                    attribute: (identifier) @func_name
                ) @full_call
            ]
        ) @call
    """,
    "javascript": """
        (call_expression
            function: [
                (identifier) @func_name
                (member_expression
                    property: (property_identifier) @func_name
                ) @full_call
            ]
        ) @call
    """,
    "typescript": """
        (call_expression
            function: [
                (identifier) @func_name
                (member_expression
                    property: (property_identifier) @func_name
                ) @full_call
            ]
        ) @call
    """,
    "go": """
        (call_expression
            function: [
                (identifier) @func_name
                (selector_expression
                    field: (field_identifier) @func_name
                ) @full_call
            ]
        ) @call
    """,
    "rust": """
        (call_expression
            function: [
                (identifier) @func_name
                (field_expression
                    field: (field_identifier) @func_name
                ) @full_call
            ]
        ) @call
    """,
}


# =============================================================================
# CodeUtils Class
# =============================================================================


class CodeUtils(REPLUtility):
    """
    Multi-language code analysis using tree-sitter (100+ languages).

    Unlike ast_utils (Python-only), code works with any language:
    JavaScript, TypeScript, Go, Rust, Java, C/C++, Ruby, Kotlin, Swift, etc.

    Methods:
        find_functions(path, language=None, name_pattern=None) -> list[FunctionDef]
            Find function/method definitions across languages
        find_classes(path, language=None, name_pattern=None) -> list[ClassDef]
            Find class/struct/interface definitions
        find_imports(path, language=None) -> list[ImportInfo]
            Find import/require/use statements
        find_calls(path, function_name, include_context=False) -> list[CallSite]
            Find all calls to a specific function
        supported_languages() -> list[str]
            List all supported language names

    Examples:
        >>> # Find all functions in any language
        >>> for f in code.find_functions("src/"):
        ...     print(f"{f.file}:{f.line} {f.name}")

        >>> # Find handler functions (regex pattern)
        >>> for f in code.find_functions(".", name_pattern="^handle"):
        ...     print(f"{f.file}:{f.line} {f.name}")

        >>> # Find classes in TypeScript files
        >>> for c in code.find_classes("src/", language="typescript"):
        ...     print(f"{c.name}: {c.methods}")

        >>> # Find calls to a function
        >>> for call in code.find_calls("src/", "fetch"):
        ...     print(f"{call.file}:{call.line}")

        >>> # Check supported languages
        >>> code.supported_languages()
        ['python', 'javascript', 'typescript', 'go', 'rust', ...]

    Language Detection:
        Language is auto-detected from file extension.
        Override with language= for ambiguous cases.

    Supported Extensions:
        .py, .js/.ts/.tsx, .go, .rs, .java, .c/.cpp, .rb, .php, .cs, .kt, .swift, ...
    """

    def __init__(self, workspace: Workspace):
        """Initialize code utilities.

        Args:
            workspace: Workspace for file access
        """
        self.workspace = workspace
        self._parser_cache: dict = {}
        self._language_cache: dict = {}

    def _get_language(self, lang: str):
        """Get cached tree-sitter language."""
        if lang not in self._language_cache:
            try:
                self._language_cache[lang] = get_language(lang)
            except Exception as e:
                raise ASTError(f"Language not supported: {lang}") from e
        return self._language_cache[lang]

    def _get_parser(self, lang: str):
        """Get cached tree-sitter parser."""
        if lang not in self._parser_cache:
            try:
                self._parser_cache[lang] = get_parser(lang)
            except Exception as e:
                raise ASTError(f"Parser not available for: {lang}") from e
        return self._parser_cache[lang]

    def _detect_language(self, path: str) -> Optional[str]:
        """Detect language from file extension.

        Args:
            path: File path

        Returns:
            Language name or None if unknown
        """
        ext = Path(path).suffix.lower()
        return EXTENSION_TO_LANGUAGE.get(ext)

    def _get_files(
        self,
        path: str,
        language: Optional[str] = None,
        recursive: bool = True,
    ) -> list[tuple[str, str]]:
        """Get files with their detected languages.

        Args:
            path: File or directory path (supports glob patterns)
            language: Filter by language (optional)
            recursive: Search recursively

        Returns:
            List of (file_path, language) tuples
        """
        # Handle glob patterns
        if "*" in path:
            files = self.workspace.glob(path)
        elif self.workspace.is_file(path):
            files = [path]
        else:
            # Directory - find all files
            pattern = "**/*" if recursive else "*"
            if path != ".":
                pattern = f"{path}/{pattern}"
            files = self.workspace.glob(pattern)

        results = []
        for file_path in files:
            detected_lang = self._detect_language(file_path)
            if detected_lang is None:
                continue
            if language and detected_lang != language:
                continue
            results.append((file_path, detected_lang))

        return results

    def _parse_file(self, path: str, language: str) -> Optional[any]:
        """Parse a file into a tree-sitter tree.

        Args:
            path: File path
            language: Tree-sitter language name

        Returns:
            Tree-sitter tree or None if parsing fails
        """
        try:
            content = self.workspace.read(path)
            parser = self._get_parser(language)
            return parser.parse(content.encode("utf-8"))
        except Exception:
            return None

    def _run_query(
        self,
        tree,
        language: str,
        query_text: str,
    ) -> list[tuple[int, dict]]:
        """Run a tree-sitter query on a tree.

        Args:
            tree: Parsed tree
            language: Language name
            query_text: Tree-sitter query

        Returns:
            List of (pattern_index, captures) tuples
        """
        try:
            lang = self._get_language(language)
            query = Query(lang, query_text)
            cursor = QueryCursor(query)
            return list(cursor.matches(tree.root_node))
        except Exception:
            return []

    def supported_languages(self) -> list[str]:
        """Get list of supported languages with file extensions.

        Returns:
            List of language names
        """
        return sorted(set(EXTENSION_TO_LANGUAGE.values()))

    def find_functions(
        self,
        path: str,
        language: Optional[str] = None,
        name_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[FunctionDef]:
        """Find function definitions across multiple languages.

        Args:
            path: File, directory, or glob pattern
            language: Filter by language (auto-detected if None)
            name_pattern: Regex pattern to filter function names
            recursive: Search subdirectories

        Returns:
            List of FunctionDef objects
        """
        import re
        pattern = re.compile(name_pattern) if name_pattern else None
        results = []

        for file_path, lang in self._get_files(path, language, recursive):
            query_text = FUNCTION_QUERIES.get(lang)
            if not query_text:
                continue

            tree = self._parse_file(file_path, lang)
            if tree is None:
                continue

            for _, captures in self._run_query(tree, lang, query_text):
                func_nodes = captures.get("func", [])
                name_nodes = captures.get("name", [])
                param_nodes = captures.get("params", [])
                return_nodes = captures.get("return_type", [])

                for i, func_node in enumerate(func_nodes):
                    name = name_nodes[i].text.decode() if i < len(name_nodes) else ""

                    # Filter by name pattern
                    if pattern and not pattern.search(name):
                        continue

                    # Extract parameters
                    params_text = ""
                    if i < len(param_nodes):
                        params_text = param_nodes[i].text.decode()

                    # Extract return type
                    return_type = None
                    if i < len(return_nodes) and return_nodes[i]:
                        return_type = return_nodes[i].text.decode()

                    # Check for async (language-specific)
                    is_async = False
                    if lang == "python":
                        is_async = func_node.type == "async_function_definition" or \
                                   any(c.type == "async" for c in func_node.children)
                    elif lang in ("javascript", "typescript"):
                        is_async = any(c.text == b"async" for c in func_node.children)

                    results.append(FunctionDef(
                        file=file_path,
                        line=func_node.start_point[0] + 1,
                        end_line=func_node.end_point[0] + 1,
                        name=name,
                        params=[p.strip() for p in params_text.strip("()").split(",") if p.strip()],
                        return_annotation=return_type,
                        is_async=is_async,
                        is_method=False,  # Would need parent context to determine
                        decorators=[],  # Would need separate query
                    ))

        return results

    def find_classes(
        self,
        path: str,
        language: Optional[str] = None,
        name_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[ClassDef]:
        """Find class/struct definitions across multiple languages.

        Args:
            path: File, directory, or glob pattern
            language: Filter by language (auto-detected if None)
            name_pattern: Regex pattern to filter class names
            recursive: Search subdirectories

        Returns:
            List of ClassDef objects
        """
        import re
        pattern = re.compile(name_pattern) if name_pattern else None
        results = []

        for file_path, lang in self._get_files(path, language, recursive):
            query_text = CLASS_QUERIES.get(lang)
            if not query_text:
                continue

            tree = self._parse_file(file_path, lang)
            if tree is None:
                continue

            for _, captures in self._run_query(tree, lang, query_text):
                class_nodes = captures.get("class", [])
                name_nodes = captures.get("name", [])
                base_nodes = captures.get("bases", [])

                for i, class_node in enumerate(class_nodes):
                    name = name_nodes[i].text.decode() if i < len(name_nodes) else ""

                    # Filter by name pattern
                    if pattern and not pattern.search(name):
                        continue

                    # Extract base classes
                    bases = []
                    if i < len(base_nodes) and base_nodes[i]:
                        bases_text = base_nodes[i].text.decode()
                        bases = [b.strip() for b in bases_text.strip("()").split(",") if b.strip()]

                    results.append(ClassDef(
                        file=file_path,
                        line=class_node.start_point[0] + 1,
                        end_line=class_node.end_point[0] + 1,
                        name=name,
                        bases=bases,
                        methods=[],  # Would need nested query
                        class_variables=[],
                    ))

        return results

    def find_imports(
        self,
        path: str,
        language: Optional[str] = None,
        module_pattern: Optional[str] = None,
        recursive: bool = True,
    ) -> list[ImportInfo]:
        """Find import statements across multiple languages.

        Args:
            path: File, directory, or glob pattern
            language: Filter by language (auto-detected if None)
            module_pattern: Regex pattern to filter module names
            recursive: Search subdirectories

        Returns:
            List of ImportInfo objects
        """
        import re
        pattern = re.compile(module_pattern) if module_pattern else None
        results = []

        for file_path, lang in self._get_files(path, language, recursive):
            query_text = IMPORT_QUERIES.get(lang)
            if not query_text:
                continue

            tree = self._parse_file(file_path, lang)
            if tree is None:
                continue

            for _, captures in self._run_query(tree, lang, query_text):
                import_nodes = captures.get("import", [])
                module_nodes = captures.get("module", [])

                for i, import_node in enumerate(import_nodes):
                    module = ""
                    if i < len(module_nodes) and module_nodes[i]:
                        module = module_nodes[i].text.decode().strip("'\"")

                    # Filter by module pattern
                    if pattern and not pattern.search(module):
                        continue

                    results.append(ImportInfo(
                        file=file_path,
                        line=import_node.start_point[0] + 1,
                        module=module,
                        names=[],  # Would need more specific query per language
                        is_from_import=lang == "python" and "from" in import_node.text.decode(),
                        is_relative=module.startswith(".") if lang == "python" else False,
                    ))

        return results

    def find_calls(
        self,
        path: str,
        function_name: str,
        language: Optional[str] = None,
        recursive: bool = True,
        include_context: bool = True,
    ) -> list[CallSite]:
        """Find function calls across multiple languages.

        Args:
            path: File, directory, or glob pattern
            function_name: Function name to find (partial match)
            language: Filter by language (auto-detected if None)
            recursive: Search subdirectories
            include_context: Include surrounding code context

        Returns:
            List of CallSite objects
        """
        results = []

        for file_path, lang in self._get_files(path, language, recursive):
            query_text = CALL_QUERIES.get(lang)
            if not query_text:
                continue

            tree = self._parse_file(file_path, lang)
            if tree is None:
                continue

            # Get file content for context
            try:
                lines = self.workspace.read_lines(file_path) if include_context else []
            except Exception:
                lines = []

            for _, captures in self._run_query(tree, lang, query_text):
                call_nodes = captures.get("call", [])
                name_nodes = captures.get("func_name", [])
                full_call_nodes = captures.get("full_call", [])

                for i, call_node in enumerate(call_nodes):
                    name = name_nodes[i].text.decode() if i < len(name_nodes) else ""

                    # Check if function name matches (partial match)
                    if function_name not in name:
                        continue

                    # Get full call expression if available
                    full_call = name
                    if i < len(full_call_nodes) and full_call_nodes[i]:
                        full_call = full_call_nodes[i].text.decode()

                    # Get context
                    context = ""
                    if lines:
                        line_idx = call_node.start_point[0]
                        if 0 <= line_idx < len(lines):
                            context = lines[line_idx].strip()

                    results.append(CallSite(
                        file=file_path,
                        line=call_node.start_point[0] + 1,
                        column=call_node.start_point[1],
                        function_name=name,
                        full_call=full_call,
                        context=context,
                    ))

        return results

    def parse(self, path: str, language: Optional[str] = None) -> Optional[any]:
        """Parse a file and return the syntax tree.

        Useful for custom analysis or debugging.

        Args:
            path: File path
            language: Language (auto-detected if None)

        Returns:
            Tree-sitter tree or None if parsing fails
        """
        lang = language or self._detect_language(path)
        if not lang:
            raise ASTError(f"Cannot detect language for: {path}")
        return self._parse_file(path, lang)

    def __repr__(self) -> str:
        return f"CodeUtils(workspace={self.workspace.root}, languages={len(self.supported_languages())})"
