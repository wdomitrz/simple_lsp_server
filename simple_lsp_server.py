#!/usr/bin/env python3
################################################################
# Copyright (c) 2026 Witalis Domitrz <witekdomitrz@gmail.com>
# AGPL License
################################################################
#
# /// script
# dependencies = [
#     "pygls",
#     "typing-extensions",
# ]
# ///

from __future__ import annotations

import argparse
import json
import logging
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, TypeAlias, cast

import lsprotocol.types as lsp_types
from pygls.server import LanguageServer
from typing_extensions import Self

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, kw_only=True)
class CommandDocument:
    source: str
    path: str
    uri: str

    @classmethod
    def from_server(cls, ls: LanguageServer, uri: str) -> Self:
        doc = ls.workspace.get_text_document(uri)
        return cls(source=doc.source, path=doc.path, uri=doc.uri)


@dataclass(frozen=True, kw_only=True)
class CommandRun:
    document: CommandDocument
    result: CommandResult


@dataclass(frozen=True, kw_only=True)
class Command:
    argv: tuple[str, ...]
    DEFAULT_TIMEOUT_SECONDS: ClassVar[float] = 10.0
    PLACEHOLDERS: ClassVar[frozenset[str]] = frozenset(
        {
            "file_path",
            "uri",
            "line",
            "character",
            "line1",
            "character1",
        },
    )
    PLACEHOLDER_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"\{(file_path|uri|line|character|line1|character1)\}",
    )

    @classmethod
    def from_string(cls, value: str) -> Self:
        """Parse a shell-style command string without using a shell.

        >>> Command.from_string("ruff format --stdin-filename {file_path} -")
        Command(argv=('ruff', 'format', '--stdin-filename', '{file_path}', '-'))
        """
        argv = tuple(shlex.split(value))
        if not argv:
            msg = "command must not be empty"
            raise ValueError(msg)
        return cls(argv=argv)

    def render(
        self,
        *,
        file_path: str,
        uri: str,
        line: int | None = None,
        character: int | None = None,
    ) -> list[str]:
        """Render command placeholders.

        >>> Command(argv=("ruff", "format", "{file_path}")).render(file_path="a.py", uri="file:///a.py")
        ['ruff', 'format', 'a.py']
        >>> Command(argv=("tool", "--uri={uri}")).render(file_path="a.py", uri="file:///a.py")
        ['tool', '--uri=file:///a.py']
        >>> Command(argv=("tool", "--line={line}", "--line1={line1}")).render(file_path="a.py", uri="file:///a.py", line=0)
        ['tool', '--line=0', '--line1=1']
        """
        values = {
            "file_path": file_path,
            "uri": uri,
            "line": "" if line is None else str(line),
            "character": "" if character is None else str(character),
            "line1": "" if line is None else str(line + 1),
            "character1": "" if character is None else str(character + 1),
        }
        return [self.render_part(part, values=values) for part in self.argv]

    @classmethod
    def render_part(cls, part: str, *, values: dict[str, str]) -> str:
        """Render known placeholders without treating other braces as format syntax.

        >>> Command.render_part("awk {print}", values={"file_path": "a.py"})
        'awk {print}'
        >>> Command.render_part("{file_path}:{line1}", values={"file_path": "a.py", "line1": "3"})
        'a.py:3'
        >>> Command.render_part("{file_path}:{line1}", values={"file_path": "{line1}.py", "line1": "3"})
        '{line1}.py:3'
        >>> Command.render_part("{file_path} {", values={"file_path": "a.py"})
        'a.py {'
        """
        return cls.PLACEHOLDER_PATTERN.sub(
            lambda match: values.get(match.group(1), ""),
            part,
        )

    def run(
        self,
        *,
        source: str,
        file_path: str,
        uri: str,
        line: int | None = None,
        character: int | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> CommandResult:
        if not self.argv:
            msg = "command must not be empty"
            raise ValueError(msg)
        argv = self.render(
            file_path=file_path,
            uri=uri,
            line=line,
            character=character,
        )
        result = subprocess.run(  # noqa: PLW1510
            argv,
            input=source,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(
            argv=argv,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


class TextRanges:
    @staticmethod
    def full_document(source: str) -> lsp_types.Range:
        """Return an LSP range covering all text in a document.

        >>> TextRanges.full_document("one\\ntwo")
        0:0-1:3
        >>> TextRanges.full_document("one\\n")
        0:0-1:0
        """
        lines = source.split("\n")
        return lsp_types.Range(
            start=lsp_types.Position(line=0, character=0),
            end=lsp_types.Position(
                line=len(lines) - 1,
                character=LspRanges.utf16_length(lines[-1]),
            ),
        )


class LspRanges:
    @staticmethod
    def utf16_length(value: str) -> int:
        """Return the LSP character length for a string.

        >>> LspRanges.utf16_length("a")
        1
        >>> LspRanges.utf16_length("😀")
        2
        """
        return len(value.encode("utf-16-le")) // 2

    @staticmethod
    def create(
        *,
        line: int,
        character: int,
        end_line: int | None = None,
        end_character: int | None = None,
    ) -> lsp_types.Range:
        if end_line is None:
            end_line = line
        if end_character is None:
            end_character = max(character + 1, 1)
        if (end_line, end_character) < (line, character):
            msg = "range end must not be before range start"
            raise ValueError(msg)
        return lsp_types.Range(
            start=lsp_types.Position(line=line, character=character),
            end=lsp_types.Position(line=end_line, character=end_character),
        )

    @staticmethod
    def overlaps(left: lsp_types.Range, right: lsp_types.Range) -> bool:
        """Return whether two LSP ranges overlap or touch.

        >>> a = lsp_types.Range(start=lsp_types.Position(line=0, character=1), end=lsp_types.Position(line=0, character=3))
        >>> b = lsp_types.Range(start=lsp_types.Position(line=0, character=2), end=lsp_types.Position(line=0, character=4))
        >>> LspRanges.overlaps(a, b)
        True
        """
        return not (
            LspRanges.is_before(left.end, right.start)
            or LspRanges.is_before(right.end, left.start)
        )

    @staticmethod
    def is_before(left: lsp_types.Position, right: lsp_types.Position) -> bool:
        return (left.line, left.character) < (right.line, right.character)

    @staticmethod
    def same(left: lsp_types.Range, right: lsp_types.Range) -> bool:
        return (
            left.start.line == right.start.line
            and left.start.character == right.start.character
            and left.end.line == right.end.line
            and left.end.character == right.end.character
        )


class JsonOutput:
    @staticmethod
    def loads(stdout: str) -> JsonValue:
        return cast(JsonValue, json.loads(stdout))

    @staticmethod
    def object(value: JsonValue, *, context: str) -> dict[str, JsonValue]:
        if not isinstance(value, dict):
            msg = f"{context} must be a JSON object"
            raise TypeError(msg)
        return value

    @staticmethod
    def list(value: JsonValue, *, context: str) -> list[JsonValue]:
        if not isinstance(value, list):
            msg = f"{context} must be a JSON list"
            raise TypeError(msg)
        return value

    @staticmethod
    def int(value: object, *, field: str) -> int:
        if isinstance(value, str):
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int):
            msg = f"{field} must be an integer"
            raise TypeError(msg)
        if value < 0:
            msg = f"{field} must be non-negative"
            raise ValueError(msg)
        return value

    @classmethod
    def one_based_int(cls, value: object, *, field: str) -> int:
        parsed = cls.int(value, field=field)
        if parsed == 0:
            msg = f"{field} must be one-based"
            raise ValueError(msg)
        return parsed - 1


@dataclass(frozen=True, kw_only=True)
class DiagnosticAndCodeAction:
    diagnostic: lsp_types.Diagnostic
    code_action: lsp_types.CodeAction | None


class CodeActionFilter:
    @classmethod
    def apply(
        cls,
        actions: list[lsp_types.CodeAction],
        *,
        request_range: lsp_types.Range,
        context_diagnostics: list[lsp_types.Diagnostic],
    ) -> list[lsp_types.CodeAction]:
        if context_diagnostics:
            return [
                action
                for action in actions
                if cls.matches_any_context_diagnostic(action, context_diagnostics)
            ]
        return [
            action
            for action in actions
            if any(
                LspRanges.overlaps(diagnostic.range, request_range)
                for diagnostic in action.diagnostics or []
            )
        ]

    @classmethod
    def matches_any_context_diagnostic(
        cls,
        action: lsp_types.CodeAction,
        context_diagnostics: list[lsp_types.Diagnostic],
    ) -> bool:
        return any(
            cls.same_diagnostic(action_diagnostic, context_diagnostic)
            for action_diagnostic in action.diagnostics or []
            for context_diagnostic in context_diagnostics
        )

    @staticmethod
    def same_diagnostic(
        left: lsp_types.Diagnostic,
        right: lsp_types.Diagnostic,
    ) -> bool:
        return (
            LspRanges.same(left.range, right.range)
            and left.message == right.message
            and left.code == right.code
            and left.source == right.source
        )


class LocationParser:
    @classmethod
    def parse_stdout(
        cls, stdout: str
    ) -> lsp_types.Location | list[lsp_types.Location] | None:
        """Parse a location, a list of locations, or null.

        Fields are zero-based.

        >>> loc = LocationParser.parse_stdout('{"uri": "file:///a.py", "line": 1, "character": 2}')
        >>> isinstance(loc, lsp_types.Location), loc.range.start.line, loc.range.start.character
        (True, 1, 2)
        """
        if not stdout.strip():
            return None
        raw = JsonOutput.loads(stdout)
        if raw is None:
            return None
        if isinstance(raw, list):
            return [cls.parse_item(item) for item in raw]
        return cls.parse_item(raw)

    @classmethod
    def parse_item(cls, item: JsonValue) -> lsp_types.Location:
        value = JsonOutput.object(item, context="location")
        return lsp_types.Location(
            uri=cls.parse_uri(value),
            range=cls.parse_range(value),
        )

    @classmethod
    def parse_uri(cls, value: dict[str, JsonValue]) -> str:
        uri = value.get("uri")
        if isinstance(uri, str):
            return uri
        file_path = value.get("file_path")
        if isinstance(file_path, str):
            return Path(file_path).expanduser().resolve().as_uri()
        msg = "location must contain uri or file_path"
        raise TypeError(msg)

    @staticmethod
    def parse_range(value: dict[str, JsonValue]) -> lsp_types.Range:
        line = JsonOutput.int(value.get("line", 0), field="line")
        character = JsonOutput.int(
            value.get("character", 0),
            field="character",
        )
        end_line = JsonOutput.int(value.get("end_line", line), field="end_line")
        end_character = JsonOutput.int(
            value.get("end_character", max(character + 1, 1)),
            field="end_character",
        )
        return LspRanges.create(
            line=line,
            character=character,
            end_line=end_line,
            end_character=end_character,
        )


class HoverParser:
    @classmethod
    def parse_stdout(cls, stdout: str) -> lsp_types.Hover | None:
        """Parse hover output as Markdown text or JSON.

        >>> HoverParser.parse_stdout("hello").contents.value
        'hello'
        >>> HoverParser.parse_stdout('{"contents": "hello", "kind": "plaintext"}').contents.kind
        <MarkupKind.PlainText: 'plaintext'>
        """
        if not stdout.strip():
            return None
        try:
            raw = JsonOutput.loads(stdout)
        except json.JSONDecodeError:
            return cls.from_text(
                stdout.rstrip("\n"), kind=lsp_types.MarkupKind.Markdown
            )

        if isinstance(raw, str):
            return cls.from_text(raw, kind=lsp_types.MarkupKind.Markdown)
        value = JsonOutput.object(raw, context="hover")
        contents = value.get("contents")
        if not isinstance(contents, str):
            msg = "hover contents must be a string"
            raise TypeError(msg)
        kind = cls.parse_markup_kind(value.get("kind", "markdown"))
        hover_range = None
        if "line" in value or "character" in value:
            hover_range = LocationParser.parse_range(value)
        return lsp_types.Hover(
            contents=lsp_types.MarkupContent(kind=kind, value=contents),
            range=hover_range,
        )

    @staticmethod
    def from_text(value: str, *, kind: lsp_types.MarkupKind) -> lsp_types.Hover:
        return lsp_types.Hover(contents=lsp_types.MarkupContent(kind=kind, value=value))

    @staticmethod
    def parse_markup_kind(value: JsonValue) -> lsp_types.MarkupKind:
        if value == "plaintext":
            return lsp_types.MarkupKind.PlainText
        if value == "markdown":
            return lsp_types.MarkupKind.Markdown
        msg = "hover kind must be markdown or plaintext"
        raise ValueError(msg)


class DocumentSymbolParser:
    @classmethod
    def parse_stdout(cls, stdout: str) -> list[lsp_types.DocumentSymbol]:
        """Parse document symbols from JSON.

        >>> symbols = DocumentSymbolParser.parse_stdout(
        ...     '[{"name": "main", "kind": "function", "line": 0, "character": 0}]'
        ... )
        >>> symbols[0].name, symbols[0].kind
        ('main', <SymbolKind.Function: 12>)
        """
        if not stdout.strip():
            return []
        raw = JsonOutput.loads(stdout)
        return [
            cls.parse_item(item) for item in JsonOutput.list(raw, context="symbols")
        ]

    @classmethod
    def parse_item(cls, item: JsonValue) -> lsp_types.DocumentSymbol:
        value = JsonOutput.object(item, context="symbol")
        name = value.get("name")
        if not isinstance(name, str) or not name:
            msg = "symbol name must be a non-empty string"
            raise TypeError(msg)
        detail = value.get("detail")
        if not isinstance(detail, str | None):
            msg = "symbol detail must be a string or null"
            raise TypeError(msg)
        symbol_range = LocationParser.parse_range(value)
        selection_range = symbol_range
        if "selection_line" in value or "selection_character" in value:
            selection_range = cls.parse_selection_range(value)
        children = None
        raw_children = value.get("children")
        if raw_children is not None:
            children = [
                cls.parse_item(child)
                for child in JsonOutput.list(raw_children, context="symbol children")
            ]

        return lsp_types.DocumentSymbol(
            name=name,
            detail=detail,
            kind=cls.parse_kind(value.get("kind", "function")),
            range=symbol_range,
            selection_range=selection_range,
            children=children,
        )

    @staticmethod
    def parse_selection_range(value: dict[str, JsonValue]) -> lsp_types.Range:
        line = JsonOutput.int(
            value.get("selection_line", value.get("line", 0)),
            field="selection_line",
        )
        character = JsonOutput.int(
            value.get("selection_character", value.get("character", 0)),
            field="selection_character",
        )
        end_line = JsonOutput.int(
            value.get("selection_end_line", line),
            field="selection_end_line",
        )
        end_character = JsonOutput.int(
            value.get("selection_end_character", max(character + 1, 1)),
            field="selection_end_character",
        )
        return LspRanges.create(
            line=line,
            character=character,
            end_line=end_line,
            end_character=end_character,
        )

    @staticmethod
    def parse_kind(value: JsonValue) -> lsp_types.SymbolKind:
        if isinstance(value, bool):
            msg = "symbol kind must be a string or integer"
            raise TypeError(msg)
        if isinstance(value, int):
            return lsp_types.SymbolKind(value)
        if not isinstance(value, str):
            msg = "symbol kind must be a string or integer"
            raise TypeError(msg)
        normalized = value.lower().replace("_", "")
        kinds = {
            "file": lsp_types.SymbolKind.File,
            "module": lsp_types.SymbolKind.Module,
            "namespace": lsp_types.SymbolKind.Namespace,
            "package": lsp_types.SymbolKind.Package,
            "class": lsp_types.SymbolKind.Class,
            "method": lsp_types.SymbolKind.Method,
            "property": lsp_types.SymbolKind.Property,
            "field": lsp_types.SymbolKind.Field,
            "constructor": lsp_types.SymbolKind.Constructor,
            "enum": lsp_types.SymbolKind.Enum,
            "interface": lsp_types.SymbolKind.Interface,
            "function": lsp_types.SymbolKind.Function,
            "variable": lsp_types.SymbolKind.Variable,
            "constant": lsp_types.SymbolKind.Constant,
            "string": lsp_types.SymbolKind.String,
            "number": lsp_types.SymbolKind.Number,
            "boolean": lsp_types.SymbolKind.Boolean,
            "array": lsp_types.SymbolKind.Array,
            "object": lsp_types.SymbolKind.Object,
            "key": lsp_types.SymbolKind.Key,
            "null": lsp_types.SymbolKind.Null,
            "enummember": lsp_types.SymbolKind.EnumMember,
            "struct": lsp_types.SymbolKind.Struct,
            "event": lsp_types.SymbolKind.Event,
            "operator": lsp_types.SymbolKind.Operator,
            "typeparameter": lsp_types.SymbolKind.TypeParameter,
        }
        try:
            return kinds[normalized]
        except KeyError as error:
            msg = f"unsupported symbol kind: {value}"
            raise ValueError(msg) from error


class ShellCheckJsonParser:
    SEVERITY_MAPPING: ClassVar[dict[str, lsp_types.DiagnosticSeverity]] = {
        "error": lsp_types.DiagnosticSeverity.Error,
        "warning": lsp_types.DiagnosticSeverity.Warning,
        "info": lsp_types.DiagnosticSeverity.Information,
        "style": lsp_types.DiagnosticSeverity.Hint,
    }

    @classmethod
    def parse_diagnostics(cls, stdout: str) -> list[lsp_types.Diagnostic]:
        """Parse diagnostics from ShellCheck json1 output.

        >>> diagnostics = ShellCheckJsonParser.parse_diagnostics(
        ...     '{"comments": [{"line": 1, "endLine": 1, "column": 6,'
        ...     ' "endColumn": 8, "level": "warning", "code": 2154,'
        ...     ' "message": "x is referenced but not assigned."}]}'
        ... )
        >>> [(d.range.start.line, d.range.start.character, d.code, d.severity) for d in diagnostics]
        [(0, 5, 2154, <DiagnosticSeverity.Warning: 2>)]
        """
        return [
            item.diagnostic
            for item in cls.parse_diagnostics_and_code_actions(
                stdout,
                file_uri="file:///unknown",
            )
        ]

    @classmethod
    def parse_code_actions(
        cls, stdout: str, *, file_uri: str
    ) -> list[lsp_types.CodeAction]:
        return [
            item.code_action
            for item in cls.parse_diagnostics_and_code_actions(
                stdout,
                file_uri=file_uri,
            )
            if item.code_action is not None
        ]

    @classmethod
    def parse_diagnostics_and_code_actions(
        cls,
        stdout: str,
        *,
        file_uri: str,
    ) -> list[DiagnosticAndCodeAction]:
        if not stdout.strip():
            return []
        raw = JsonOutput.object(JsonOutput.loads(stdout), context="shellcheck output")
        comments = JsonOutput.list(
            raw.get("comments", []), context="shellcheck comments"
        )
        return [cls.parse_comment(comment, file_uri=file_uri) for comment in comments]

    @classmethod
    def parse_comment(
        cls,
        comment: JsonValue,
        *,
        file_uri: str,
    ) -> DiagnosticAndCodeAction:
        value = JsonOutput.object(comment, context="shellcheck comment")
        diagnostic = cls.parse_diagnostic(value)
        return DiagnosticAndCodeAction(
            diagnostic=diagnostic,
            code_action=cls.parse_code_action(
                value,
                file_uri=file_uri,
                diagnostic=diagnostic,
            ),
        )

    @classmethod
    def parse_diagnostic(cls, value: dict[str, JsonValue]) -> lsp_types.Diagnostic:
        line = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(value.get("line"), field="line"),
            field="line",
        )
        character = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(value.get("column"), field="column"),
            field="column",
        )
        end_line = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(value.get("endLine"), field="endLine"),
            field="endLine",
        )
        end_character = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(value.get("endColumn"), field="endColumn"),
            field="endColumn",
        )
        message = value.get("message")
        if not isinstance(message, str) or not message:
            msg = "shellcheck message must be a non-empty string"
            raise TypeError(msg)
        code = DiagnosticParser.as_int(value.get("code"), field="code")
        return lsp_types.Diagnostic(
            range=LspRanges.create(
                line=line,
                character=character,
                end_line=end_line,
                end_character=end_character,
            ),
            message=message,
            severity=cls.parse_severity(value.get("level")),
            code=code,
            code_description=lsp_types.CodeDescription(
                href=f"https://www.shellcheck.net/wiki/SC{code}",
            ),
            source="shellcheck",
        )

    @classmethod
    def parse_code_action(
        cls,
        value: dict[str, JsonValue],
        *,
        file_uri: str,
        diagnostic: lsp_types.Diagnostic,
    ) -> lsp_types.CodeAction | None:
        fix = value.get("fix")
        if fix is None:
            return None
        fix_object = JsonOutput.object(fix, context="shellcheck fix")
        replacements = [
            cls.parse_replacement(replacement)
            for replacement in JsonOutput.list(
                fix_object.get("replacements", []),
                context="shellcheck replacements",
            )
        ]
        if not replacements:
            return None

        return lsp_types.CodeAction(
            title=diagnostic.message,
            diagnostics=[diagnostic],
            kind=lsp_types.CodeActionKind.QuickFix,
            is_preferred=True,
            edit=lsp_types.WorkspaceEdit(
                document_changes=cast(
                    list[
                        lsp_types.TextDocumentEdit
                        | lsp_types.CreateFile
                        | lsp_types.RenameFile
                        | lsp_types.DeleteFile
                    ],
                    [
                        lsp_types.TextDocumentEdit(
                            text_document=lsp_types.OptionalVersionedTextDocumentIdentifier(
                                uri=file_uri,
                            ),
                            edits=cast(
                                list[lsp_types.TextEdit | lsp_types.AnnotatedTextEdit],
                                replacements,
                            ),
                        ),
                    ],
                ),
            ),
        )

    @staticmethod
    def parse_replacement(value: JsonValue) -> lsp_types.TextEdit:
        replacement = JsonOutput.object(value, context="shellcheck replacement")
        line = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(replacement.get("line"), field="line"),
            field="line",
        )
        character = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(replacement.get("column"), field="column"),
            field="column",
        )
        end_line = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(replacement.get("endLine"), field="endLine"),
            field="endLine",
        )
        end_character = DiagnosticParser.one_based_to_zero_based(
            DiagnosticParser.as_int(replacement.get("endColumn"), field="endColumn"),
            field="endColumn",
        )
        replacement_text = replacement.get("replacement")
        if not isinstance(replacement_text, str):
            msg = "shellcheck replacement text must be a string"
            raise TypeError(msg)
        return lsp_types.TextEdit(
            range=LspRanges.create(
                line=line,
                character=character,
                end_line=end_line,
                end_character=end_character,
            ),
            new_text=replacement_text,
        )

    @classmethod
    def parse_severity(cls, value: JsonValue) -> lsp_types.DiagnosticSeverity:
        if not isinstance(value, str):
            msg = "shellcheck level must be a string"
            raise TypeError(msg)
        try:
            return cls.SEVERITY_MAPPING[value]
        except KeyError as error:
            msg = f"unsupported shellcheck level: {value}"
            raise ValueError(msg) from error


class DiagnosticParser:
    DEFAULT_SOURCE: ClassVar[str] = "simple-lsp-server"
    GCC_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?P<path>.*?):(?P<line>\d+):(?P<character>\d+):\s+(?:(?P<severity>error|warning|note|info|hint):\s+)?(?P<message>.*)$",
    )
    MARKDOWNLINT_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^(?P<path>.*?):(?P<line>\d+)(?::(?P<character>\d+))?\s+(?P<code>MD\d{3}(?:/[A-Za-z0-9_-]+)?)\s+(?P<message>.*)$",
    )

    @classmethod
    def parse_stdout(cls, stdout: str) -> list[lsp_types.Diagnostic]:
        """Parse command stdout as JSON or GCC-style diagnostics.

        Custom JSON line and character fields are zero-based. Ruff JSON and
        GCC-style output are converted from one-based positions.

        >>> diagnostics = DiagnosticParser.parse_stdout(
        ...     '[{"line": 2, "character": 4, "message": "bad", "severity": "warning"}]'
        ... )
        >>> [(d.range.start.line, d.range.start.character, d.message, d.severity) for d in diagnostics]
        [(2, 4, 'bad', <DiagnosticSeverity.Warning: 2>)]
        >>> ruff = DiagnosticParser.parse_stdout(
        ...     '[{"code": "F401", "location": {"row": 1, "column": 8},'
        ...     ' "end_location": {"row": 1, "column": 10}, "message": "unused",'
        ...     ' "severity": "error"}]'
        ... )
        >>> [(d.range.start.line, d.range.start.character, d.code) for d in ruff]
        [(0, 7, 'F401')]
        >>> gcc = DiagnosticParser.parse_stdout("-:1:6: warning: bad [SC1234]\\n")
        >>> [(d.range.start.line, d.range.start.character, d.message, d.code) for d in gcc]
        [(0, 5, 'bad', 'SC1234')]
        >>> md = DiagnosticParser.parse_stdout("README.md:2 MD041/first-line-heading First line\\n")
        >>> [(d.range.start.line, d.range.start.character, d.message, d.code) for d in md]
        [(1, 0, 'First line', 'MD041')]
        >>> mdl_json = DiagnosticParser.parse_stdout(
        ...     '[{"filename": "README.md", "line": 2, "rule": "MD041",'
        ...     ' "description": "First line"}]'
        ... )
        >>> [(d.range.start.line, d.range.start.character, d.message, d.code) for d in mdl_json]
        [(1, 0, 'First line', 'MD041')]
        >>> shellcheck = DiagnosticParser.parse_stdout(
        ...     '{"comments": [{"line": 1, "endLine": 1, "column": 6,'
        ...     ' "endColumn": 8, "level": "warning", "code": 2154,'
        ...     ' "message": "x is referenced but not assigned."}]}'
        ... )
        >>> [(d.range.start.line, d.range.start.character, d.code) for d in shellcheck]
        [(0, 5, 2154)]
        >>> DiagnosticParser.parse_stdout("")
        []
        """
        if not stdout.strip():
            return []

        try:
            raw = cast(JsonValue, json.loads(stdout))
        except json.JSONDecodeError:
            return cls.parse_gcc_lines(stdout)

        if isinstance(raw, dict):
            if "comments" in raw:
                return ShellCheckJsonParser.parse_diagnostics(stdout)
            raw = raw.get("diagnostics", [])
        if not isinstance(raw, list):
            msg = "diagnostics output must be a JSON list or object with a diagnostics list"
            raise TypeError(msg)

        return [cls.parse_json_item(item) for item in raw]

    @classmethod
    def parse_json_item(cls, item: object) -> lsp_types.Diagnostic:
        if not isinstance(item, dict):
            msg = "each diagnostic must be a JSON object"
            raise TypeError(msg)

        json_item = cast(dict[str, JsonValue], item)
        if "location" in json_item:
            return cls.parse_ruff_item(json_item)
        if "rule" in json_item and "description" in json_item:
            return cls.parse_markdownlint_json_item(json_item)
        return cls.parse_custom_json_item(json_item)

    @classmethod
    def parse_custom_json_item(cls, item: dict[str, JsonValue]) -> lsp_types.Diagnostic:
        line = cls.as_int(item.get("line", 0), field="line")
        character = cls.as_int(item.get("character", 0), field="character")
        end_line = cls.as_int(item.get("end_line", line), field="end_line")
        end_character = cls.as_int(
            item.get("end_character", max(character + 1, 1)),
            field="end_character",
        )
        message = item.get("message")
        if not isinstance(message, str) or not message:
            msg = "diagnostic message must be a non-empty string"
            raise TypeError(msg)

        source = item.get("source", cls.DEFAULT_SOURCE)
        if not isinstance(source, str):
            msg = "diagnostic source must be a string"
            raise TypeError(msg)

        code = item.get("code")
        if not isinstance(code, str | int | None):
            msg = "diagnostic code must be a string, integer, or null"
            raise TypeError(msg)

        severity = cls.parse_severity(item.get("severity", "error"))
        return lsp_types.Diagnostic(
            range=LspRanges.create(
                line=line,
                character=character,
                end_line=end_line,
                end_character=end_character,
            ),
            message=message,
            severity=severity,
            source=source,
            code=code,
        )

    @classmethod
    def parse_ruff_item(cls, item: dict[str, JsonValue]) -> lsp_types.Diagnostic:
        location = item.get("location")
        end_location = item.get("end_location", location)
        if not isinstance(location, dict) or not isinstance(end_location, dict):
            msg = "ruff diagnostic locations must be objects"
            raise TypeError(msg)

        line = cls.one_based_to_zero_based(
            cls.as_int(location.get("row"), field="location.row"),
            field="location.row",
        )
        character = cls.one_based_to_zero_based(
            cls.as_int(location.get("column"), field="location.column"),
            field="location.column",
        )
        end_line = cls.one_based_to_zero_based(
            cls.as_int(end_location.get("row"), field="end_location.row"),
            field="end_location.row",
        )
        end_character = cls.one_based_to_zero_based(
            cls.as_int(end_location.get("column"), field="end_location.column"),
            field="end_location.column",
        )

        message = item.get("message")
        if not isinstance(message, str) or not message:
            msg = "ruff diagnostic message must be a non-empty string"
            raise TypeError(msg)

        code = item.get("code")
        if not isinstance(code, str | None):
            msg = "ruff diagnostic code must be a string or null"
            raise TypeError(msg)

        return lsp_types.Diagnostic(
            range=LspRanges.create(
                line=line,
                character=character,
                end_line=end_line,
                end_character=end_character,
            ),
            message=message,
            severity=cls.parse_severity(item.get("severity", "error")),
            source="ruff",
            code=code,
        )

    @classmethod
    def parse_markdownlint_json_item(
        cls, item: dict[str, JsonValue]
    ) -> lsp_types.Diagnostic:
        line = cls.one_based_to_zero_based(
            cls.as_int(item.get("line"), field="line"),
            field="line",
        )
        filename = item.get("filename")
        if not isinstance(filename, str):
            msg = "markdownlint filename must be a string"
            raise TypeError(msg)
        rule = item.get("rule")
        if not isinstance(rule, str):
            msg = "markdownlint rule must be a string"
            raise TypeError(msg)
        description = item.get("description")
        if not isinstance(description, str):
            msg = "markdownlint description must be a string"
            raise TypeError(msg)

        return lsp_types.Diagnostic(
            range=LspRanges.create(line=line, character=0),
            message=description,
            severity=lsp_types.DiagnosticSeverity.Warning,
            source=filename,
            code=rule,
        )

    @classmethod
    def parse_gcc_lines(cls, stdout: str) -> list[lsp_types.Diagnostic]:
        diagnostics: list[lsp_types.Diagnostic] = []
        for line in stdout.splitlines():
            if not line.strip():
                continue
            markdownlint_match = cls.MARKDOWNLINT_PATTERN.match(line)
            if markdownlint_match is not None:
                diagnostics.append(cls.parse_markdownlint_match(markdownlint_match))
                continue
            match = cls.GCC_PATTERN.match(line)
            if match is None:
                msg = f"cannot parse diagnostic line: {line}"
                raise ValueError(msg)
            diagnostics.append(cls.parse_gcc_match(match))
        return diagnostics

    @classmethod
    def parse_gcc_match(cls, match: re.Match[str]) -> lsp_types.Diagnostic:
        line = cls.one_based_to_zero_based(
            cls.as_int(match.group("line"), field="line"),
            field="line",
        )
        character = cls.one_based_to_zero_based(
            cls.as_int(match.group("character"), field="character"),
            field="character",
        )
        message = match.group("message")
        code_match = re.search(r"\[(?P<code>[A-Za-z]+\d+)\]$", message)
        code = None if code_match is None else code_match.group("code")
        if code_match is not None:
            message = message[: code_match.start()].rstrip()

        return lsp_types.Diagnostic(
            range=LspRanges.create(line=line, character=character),
            message=message,
            severity=cls.parse_gcc_severity(match.group("severity")),
            source=match.group("path"),
            code=code,
        )

    @classmethod
    def parse_markdownlint_match(cls, match: re.Match[str]) -> lsp_types.Diagnostic:
        line = cls.one_based_to_zero_based(
            cls.as_int(match.group("line"), field="line"),
            field="line",
        )
        character_text = match.group("character")
        character = 0
        if character_text is not None:
            character = cls.one_based_to_zero_based(
                cls.as_int(character_text, field="character"),
                field="character",
            )
        code = match.group("code").split("/", maxsplit=1)[0]

        return lsp_types.Diagnostic(
            range=LspRanges.create(line=line, character=character),
            message=match.group("message"),
            severity=lsp_types.DiagnosticSeverity.Warning,
            source=match.group("path"),
            code=code,
        )

    @staticmethod
    def as_int(value: object, *, field: str) -> int:
        return JsonOutput.int(value, field=f"diagnostic {field}")

    @staticmethod
    def one_based_to_zero_based(value: int, *, field: str) -> int:
        return JsonOutput.one_based_int(value, field=f"diagnostic {field}")

    @classmethod
    def parse_gcc_severity(cls, value: str | None) -> lsp_types.DiagnosticSeverity:
        if value == "note":
            return lsp_types.DiagnosticSeverity.Information
        if value == "info":
            return lsp_types.DiagnosticSeverity.Information
        if value is None:
            return lsp_types.DiagnosticSeverity.Error
        return cls.parse_severity(value)

    @staticmethod
    def parse_severity(value: object) -> lsp_types.DiagnosticSeverity:
        """Parse LSP diagnostic severities.

        >>> DiagnosticParser.parse_severity("error")
        <DiagnosticSeverity.Error: 1>
        >>> DiagnosticParser.parse_severity(2)
        <DiagnosticSeverity.Warning: 2>
        >>> DiagnosticParser.parse_severity("info")
        <DiagnosticSeverity.Information: 3>
        """
        if isinstance(value, bool):
            msg = "diagnostic severity must be a string or integer"
            raise TypeError(msg)
        if isinstance(value, int):
            return lsp_types.DiagnosticSeverity(value)
        if not isinstance(value, str):
            msg = "diagnostic severity must be a string or integer"
            raise TypeError(msg)

        severities = {
            "error": lsp_types.DiagnosticSeverity.Error,
            "warning": lsp_types.DiagnosticSeverity.Warning,
            "information": lsp_types.DiagnosticSeverity.Information,
            "info": lsp_types.DiagnosticSeverity.Information,
            "note": lsp_types.DiagnosticSeverity.Information,
            "hint": lsp_types.DiagnosticSeverity.Hint,
            "style": lsp_types.DiagnosticSeverity.Hint,
        }
        try:
            return severities[value.lower()]
        except KeyError as error:
            msg = f"unsupported diagnostic severity: {value}"
            raise ValueError(msg) from error


@dataclass(frozen=True, kw_only=True)
class ServerConfig:
    format_command: Command | None
    diagnostics_command: Command | None
    code_actions_command: Command | None
    hover_command: Command | None
    definition_command: Command | None
    references_command: Command | None
    document_symbols_command: Command | None
    diagnostics_on_change: bool

    def validate(self) -> None:
        commands = (
            self.format_command,
            self.diagnostics_command,
            self.code_actions_command,
            self.hover_command,
            self.definition_command,
            self.references_command,
            self.document_symbols_command,
        )
        if all(command is None for command in commands):
            msg = "at least one LSP command option is required"
            raise ValueError(msg)

    def create_server(self) -> LanguageServer:
        self.validate()
        server = LanguageServer("simple-command-lsp", "0.1")

        if self.format_command is not None:
            self.register_formatting(server, self.format_command)

        if self.diagnostics_command is not None:
            self.register_diagnostics(server, self.diagnostics_command)

        if self.code_actions_command is not None:
            self.register_code_actions(server, self.code_actions_command)

        if self.hover_command is not None:
            self.register_hover(server, self.hover_command)

        if self.definition_command is not None:
            self.register_definition(server, self.definition_command)

        if self.references_command is not None:
            self.register_references(server, self.references_command)

        if self.document_symbols_command is not None:
            self.register_document_symbols(server, self.document_symbols_command)

        return server

    def run_document_command(
        self,
        ls: LanguageServer,
        *,
        command: Command,
        uri: str,
        title: str,
        allow_failure_with_stdout: bool = False,
        line: int | None = None,
        character: int | None = None,
    ) -> CommandRun | None:
        document: CommandDocument | None = None
        try:
            document = CommandDocument.from_server(ls, uri)
            result = command.run(
                source=document.source,
                file_path=document.path,
                uri=document.uri,
                line=line,
                character=character,
            )
            if result.returncode != 0 and (
                not allow_failure_with_stdout or not result.stdout.strip()
            ):
                raise subprocess.CalledProcessError(
                    result.returncode,
                    result.argv,
                    output=result.stdout,
                    stderr=result.stderr,
                )
        except (
            KeyError,
            OSError,
            subprocess.SubprocessError,
            TypeError,
            ValueError,
        ) as error:
            self.log_command_error(
                ls,
                title=title,
                command=command,
                file_path=uri if document is None else document.path,
                error=error,
            )
            return None
        return CommandRun(document=document, result=result)

    def register_formatting(self, server: LanguageServer, command: Command) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_FORMATTING)
        def formatting(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DocumentFormattingParams,
        ) -> list[lsp_types.TextEdit]:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_FORMATTING, params)
            run = self.run_document_command(
                ls,
                command=command,
                uri=params.text_document.uri,
                title="formatter error",
            )
            if run is None:
                return []

            return [
                lsp_types.TextEdit(
                    range=TextRanges.full_document(run.document.source),
                    new_text=run.result.stdout,
                ),
            ]

    def register_diagnostics(self, server: LanguageServer, command: Command) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_DID_OPEN)
        def did_open(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DidOpenTextDocumentParams,
        ) -> None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_DID_OPEN, params)
            self.publish_diagnostics(ls, params.text_document.uri, command=command)

        @server.feature(lsp_types.TEXT_DOCUMENT_DID_SAVE)
        def did_save(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DidSaveTextDocumentParams,
        ) -> None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_DID_SAVE, params)
            self.publish_diagnostics(ls, params.text_document.uri, command=command)

        if not self.diagnostics_on_change:
            return

        @server.feature(lsp_types.TEXT_DOCUMENT_DID_CHANGE)
        def did_change(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DidChangeTextDocumentParams,
        ) -> None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_DID_CHANGE, params)
            self.publish_diagnostics(ls, params.text_document.uri, command=command)

    def register_code_actions(self, server: LanguageServer, command: Command) -> None:
        @server.feature(
            lsp_types.TEXT_DOCUMENT_CODE_ACTION,
            lsp_types.CodeActionOptions(
                code_action_kinds=[lsp_types.CodeActionKind.QuickFix]
            ),
        )
        def code_actions(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.CodeActionParams,
        ) -> list[lsp_types.CodeAction]:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_CODE_ACTION, params)
            try:
                run = self.run_document_command(
                    ls,
                    command=command,
                    uri=params.text_document.uri,
                    title="code actions error",
                    allow_failure_with_stdout=True,
                )
                if run is None:
                    return []
                actions = ShellCheckJsonParser.parse_code_actions(
                    run.result.stdout,
                    file_uri=run.document.uri,
                )
                return CodeActionFilter.apply(
                    actions,
                    request_range=params.range,
                    context_diagnostics=params.context.diagnostics,
                )
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                self.log_command_error(
                    ls,
                    title="code actions error",
                    command=command,
                    file_path=params.text_document.uri,
                    error=error,
                )
                return []

    def register_hover(self, server: LanguageServer, command: Command) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_HOVER)
        def hover(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.HoverParams,
        ) -> lsp_types.Hover | None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_HOVER, params)
            try:
                run = self.run_document_command(
                    ls,
                    command=command,
                    uri=params.text_document.uri,
                    title="hover error",
                    line=params.position.line,
                    character=params.position.character,
                )
                if run is None:
                    return None
                return HoverParser.parse_stdout(run.result.stdout)
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                self.log_command_error(
                    ls,
                    title="hover error",
                    command=command,
                    file_path=params.text_document.uri,
                    error=error,
                )
                return None

    def register_definition(self, server: LanguageServer, command: Command) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_DEFINITION)
        def definition(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DefinitionParams,
        ) -> lsp_types.Location | list[lsp_types.Location] | None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_DEFINITION, params)
            return self.run_location_command(
                ls,
                command=command,
                uri=params.text_document.uri,
                line=params.position.line,
                character=params.position.character,
                title="definition error",
            )

    def register_references(self, server: LanguageServer, command: Command) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_REFERENCES)
        def references(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.ReferenceParams,
        ) -> list[lsp_types.Location] | None:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_REFERENCES, params)
            locations = self.run_location_command(
                ls,
                command=command,
                uri=params.text_document.uri,
                line=params.position.line,
                character=params.position.character,
                title="references error",
            )
            if isinstance(locations, lsp_types.Location):
                return [locations]
            return locations

    def register_document_symbols(
        self, server: LanguageServer, command: Command
    ) -> None:
        @server.feature(lsp_types.TEXT_DOCUMENT_DOCUMENT_SYMBOL)
        def document_symbols(  # pyright: ignore[reportUnusedFunction]
            ls: LanguageServer,
            params: lsp_types.DocumentSymbolParams,
        ) -> list[lsp_types.DocumentSymbol]:
            logging.debug("%s %s", lsp_types.TEXT_DOCUMENT_DOCUMENT_SYMBOL, params)
            try:
                run = self.run_document_command(
                    ls,
                    command=command,
                    uri=params.text_document.uri,
                    title="document symbols error",
                )
                if run is None:
                    return []
                return DocumentSymbolParser.parse_stdout(run.result.stdout)
            except (
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as error:
                self.log_command_error(
                    ls,
                    title="document symbols error",
                    command=command,
                    file_path=params.text_document.uri,
                    error=error,
                )
                return []

    def run_location_command(
        self,
        ls: LanguageServer,
        *,
        command: Command,
        uri: str,
        line: int,
        character: int,
        title: str,
    ) -> lsp_types.Location | list[lsp_types.Location] | None:
        try:
            run = self.run_document_command(
                ls,
                command=command,
                uri=uri,
                title=title,
                line=line,
                character=character,
            )
            if run is None:
                return None
            return LocationParser.parse_stdout(run.result.stdout)
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            self.log_command_error(
                ls,
                title=title,
                command=command,
                file_path=uri,
                error=error,
            )
            return None

    def publish_diagnostics(
        self,
        ls: LanguageServer,
        uri: str,
        *,
        command: Command,
    ) -> None:
        try:
            run = self.run_document_command(
                ls,
                command=command,
                uri=uri,
                title="diagnostics command failed",
                allow_failure_with_stdout=True,
            )
            if run is None:
                return
            diagnostics = DiagnosticParser.parse_stdout(run.result.stdout)
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as error:
            self.log_command_error(
                ls,
                title="diagnostics error",
                command=command,
                file_path=uri,
                error=error,
            )
            return

        ls.publish_diagnostics(uri, diagnostics)  # pyright: ignore[reportUnknownMemberType]

    @staticmethod
    def log_command_error(
        ls: LanguageServer,
        *,
        title: str,
        command: Command,
        file_path: str,
        error: BaseException,
    ) -> None:
        error_info = {
            "command": command.argv,
            "file": file_path,
            "stderr": getattr(error, "stderr", None),
            "error": str(error),
        }
        logging.exception("%s: %s", title, error_info)
        ls.show_message_log(f"{title}: {error_info}")  # pyright: ignore[reportUnknownMemberType]


@dataclass(frozen=True, kw_only=True)
class Args:
    format_command: str | None
    diagnostics_command: str | None
    code_actions_command: str | None
    hover_command: str | None
    definition_command: str | None
    references_command: str | None
    document_symbols_command: str | None
    diagnostics_on_change: bool
    log_level: str

    @classmethod
    def from_args(cls, argv: list[str] | None = None) -> Self:
        parser = argparse.ArgumentParser(
            description="Expose external commands through a small Language Server Protocol server.",
        )
        _ = parser.add_argument(
            "--format-command",
            help=(
                "formatter command. It receives the document on stdin and should write "
                "the full formatted document to stdout. Supports {file_path} and {uri}."
            ),
        )
        _ = parser.add_argument(
            "--diagnostics-command",
            help=(
                "diagnostics command. It receives the document on stdin and should write "
                "JSON diagnostics to stdout. Supports {file_path} and {uri}."
            ),
        )
        _ = parser.add_argument(
            "--code-actions-command",
            help=(
                "code actions command. It receives the document on stdin and should "
                "write ShellCheck json1 output to stdout. Supports {file_path} and {uri}."
            ),
        )
        _ = parser.add_argument(
            "--hover-command",
            help=(
                "hover command. It receives the document on stdin and should write "
                "Markdown/plain text or hover JSON to stdout. Supports {file_path}, "
                "{uri}, {line}, {character}, {line1}, and {character1}."
            ),
        )
        _ = parser.add_argument(
            "--definition-command",
            help=(
                "definition command. It receives the document on stdin and should write "
                "a JSON location, a JSON location list, or null. Supports {file_path}, "
                "{uri}, {line}, {character}, {line1}, and {character1}."
            ),
        )
        _ = parser.add_argument(
            "--references-command",
            help=(
                "references command. It receives the document on stdin and should write "
                "a JSON location list. Supports {file_path}, {uri}, {line}, "
                "{character}, {line1}, and {character1}."
            ),
        )
        _ = parser.add_argument(
            "--document-symbols-command",
            help=(
                "document symbols command. It receives the document on stdin and should "
                "write a JSON list of document symbols."
            ),
        )
        _ = parser.add_argument(
            "--diagnostics-on-change",
            action="store_true",
            help="run diagnostics on every textDocument/didChange notification",
        )
        _ = parser.add_argument(
            "--log-level",
            default="WARNING",
            choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        )
        args = parser.parse_args(argv)
        return cls(
            format_command=cast(str | None, args.format_command),
            diagnostics_command=cast(str | None, args.diagnostics_command),
            code_actions_command=cast(str | None, args.code_actions_command),
            hover_command=cast(str | None, args.hover_command),
            definition_command=cast(str | None, args.definition_command),
            references_command=cast(str | None, args.references_command),
            document_symbols_command=cast(str | None, args.document_symbols_command),
            diagnostics_on_change=cast(bool, args.diagnostics_on_change),
            log_level=cast(str, args.log_level),
        )

    def config(self) -> ServerConfig:
        return ServerConfig(
            format_command=self.to_command(self.format_command),
            diagnostics_command=self.to_command(self.diagnostics_command),
            code_actions_command=self.to_command(self.code_actions_command),
            hover_command=self.to_command(self.hover_command),
            definition_command=self.to_command(self.definition_command),
            references_command=self.to_command(self.references_command),
            document_symbols_command=self.to_command(self.document_symbols_command),
            diagnostics_on_change=self.diagnostics_on_change,
        )

    def run(self) -> int:
        logging.basicConfig(level=self.log_level)
        server = self.config().create_server()
        server.start_io()
        return 0

    @staticmethod
    def to_command(value: str | None) -> Command | None:
        if value is None:
            return None
        return Command.from_string(value)


if __name__ == "__main__":
    raise SystemExit(Args.from_args().run())
