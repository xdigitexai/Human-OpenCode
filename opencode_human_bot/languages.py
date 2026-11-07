from __future__ import annotations

import importlib
import importlib.util
import shutil
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LanguageAdapter:
    name: str
    extensions: tuple[str, ...]
    parser_id: str
    function_nodes: tuple[str, ...]
    class_nodes: tuple[str, ...]
    dependencies: tuple[str, ...]
    compiler: tuple[str, ...]
    tests: tuple[str, ...]

    @property
    def parser_available(self) -> bool:
        return importlib.util.find_spec("tree_sitter_language_pack") is not None

    def parse(self, source: bytes) -> tuple[list[dict[str, Any]], str | None]:
        try:
            pack = importlib.import_module("tree_sitter_language_pack")
            parser = pack.get_parser(self.parser_id)
            tree = parser.parse(source)
        except (ImportError, AttributeError, LookupError, RuntimeError) as error:
            return [], f"Tree-sitter parser unavailable: {error}"

        symbols: list[dict[str, Any]] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            node_type = str(node.type)
            kind = ("function" if node_type in self.function_nodes else
                    "class" if node_type in self.class_nodes else None)
            if kind:
                name_node = next((child for child in node.children
                                  if child.type in {"identifier", "name", "type_identifier"}), None)
                name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace") if name_node else ""
                symbols.append({"kind": kind, "name": name, "line": node.start_point.row + 1,
                                "end_line": node.end_point.row + 1,
                                "source": source[node.start_byte:node.end_byte].decode("utf-8", "replace")})
            stack.extend(reversed(node.children))
        return symbols, None

    def imports(self, source: str) -> list[str]:
        import re

        patterns = {
            "Python": r"(?m)^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
            "JavaScript": r"(?m)^\s*(?:import\s+.*?\s+from\s+|export\s+.*?\s+from\s+|require\()\s*['\"]([^'\"]+)",
            "TypeScript": r"(?m)^\s*(?:import\s+.*?\s+from\s+|export\s+.*?\s+from\s+|require\()\s*['\"]([^'\"]+)",
            "PHP": r"(?m)^\s*(?:use|require_once|include_once)\s+([^;]+)",
            "Rust": r"(?m)^\s*use\s+([^;]+)",
            "Go": r"(?m)^\s*import\s+(?:\([^)]*\)|['\"]([^'\"]+))",
            "Java": r"(?m)^\s*import\s+(?:static\s+)?([\w.*]+);",
            "C": r"(?m)^\s*#include\s*[<\"]([^>\"]+)",
            "C++": r"(?m)^\s*#include\s*[<\"]([^>\"]+)",
            "C#": r"(?m)^\s*using\s+([\w.]+);",
            "Ruby": r"(?m)^\s*require(?:_relative)?\s+['\"]([^'\"]+)",
            "Kotlin": r"(?m)^\s*import\s+([\w.*]+)",
            "Swift": r"(?m)^\s*import\s+([\w.]+)",
        }
        return sorted({group for match in re.finditer(patterns[self.name], source)
                       for group in match.groups() if group})


def _adapter(name: str, extensions: tuple[str, ...], parser_id: str,
             functions: tuple[str, ...], classes: tuple[str, ...], deps: tuple[str, ...],
             compiler: tuple[str, ...], tests: tuple[str, ...]) -> LanguageAdapter:
    return LanguageAdapter(name, extensions, parser_id, functions, classes, deps, compiler, tests)


ADAPTERS = (
    _adapter("Python", (".py", ".pyi"), "python", ("function_definition",), ("class_definition",), ("pyproject.toml", "requirements.txt", "Pipfile", "setup.py"), ("python", "-m", "compileall"), ("pytest",)),
    _adapter("JavaScript", (".js", ".jsx", ".mjs", ".cjs"), "javascript", ("function_declaration", "method_definition"), ("class_declaration",), ("package.json",), ("node", "--check"), ("npm", "test")),
    _adapter("TypeScript", (".ts", ".tsx", ".mts", ".cts"), "typescript", ("function_declaration", "method_definition"), ("class_declaration",), ("package.json", "tsconfig.json"), ("npx", "tsc", "--noEmit"), ("npm", "test")),
    _adapter("PHP", (".php",), "php", ("function_definition", "method_declaration"), ("class_declaration", "interface_declaration", "trait_declaration"), ("composer.json",), ("php", "-l"), ("vendor/bin/phpunit",)),
    _adapter("Rust", (".rs",), "rust", ("function_item",), ("struct_item", "enum_item", "trait_item"), ("Cargo.toml",), ("cargo", "check"), ("cargo", "test")),
    _adapter("Go", (".go",), "go", ("function_declaration", "method_declaration"), ("type_declaration",), ("go.mod",), ("go", "build", "./..."), ("go", "test", "./...")),
    _adapter("Java", (".java",), "java", ("method_declaration", "constructor_declaration"), ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"), ("pom.xml", "build.gradle", "build.gradle.kts"), ("javac",), ("mvn", "test")),
    _adapter("C", (".c", ".h"), "c", ("function_definition",), ("struct_specifier", "enum_specifier"), ("CMakeLists.txt", "Makefile"), ("gcc", "-fsyntax-only"), ("ctest",)),
    _adapter("C++", (".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx"), "cpp", ("function_definition",), ("class_specifier", "struct_specifier"), ("CMakeLists.txt", "Makefile"), ("g++", "-fsyntax-only"), ("ctest",)),
    _adapter("C#", (".cs",), "csharp", ("method_declaration", "local_function_statement"), ("class_declaration", "interface_declaration", "struct_declaration", "record_declaration"), ("*.csproj", "*.sln"), ("dotnet", "build"), ("dotnet", "test")),
    _adapter("Ruby", (".rb",), "ruby", ("method", "singleton_method"), ("class", "module"), ("Gemfile", "*.gemspec"), ("ruby", "-c"), ("bundle", "exec", "rspec")),
    _adapter("Kotlin", (".kt", ".kts"), "kotlin", ("function_declaration",), ("class_declaration", "object_declaration", "interface_declaration"), ("build.gradle.kts", "pom.xml"), ("kotlinc",), ("gradle", "test")),
    _adapter("Swift", (".swift",), "swift", ("function_declaration", "init_declaration"), ("class_declaration", "struct_declaration", "protocol_declaration", "enum_declaration"), ("Package.swift",), ("swift", "build"), ("swift", "test")),
)

BY_EXTENSION = {extension: adapter for adapter in ADAPTERS for extension in adapter.extensions}


def adapter_for_path(path):
    return BY_EXTENSION.get(path.suffix.lower())


def available_commands(adapter: LanguageAdapter) -> dict[str, bool]:
    return {command: shutil.which(command) is not None
            for command in sorted({adapter.compiler[0], adapter.tests[0]})}