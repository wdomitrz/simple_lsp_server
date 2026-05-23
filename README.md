Simple command-backed LSP server.

The server exposes external commands through practical LSP features:

- document formatting
- diagnostics
- quick-fix code actions
- hover
- go to definition
- find references
- document symbols

Commands are passed as shell-style strings, parsed with `shlex.split`, and then
executed without a shell. Commands receive the document text on stdin. All
commands support `{file_path}` and `{uri}` placeholders. Position-aware commands
also support zero-based `{line}` and `{character}` plus one-based `{line1}` and
`{character1}`.

Examples:

```sh
./simple_lsp_server.py \
  --format-command 'ruff format --stdin-filename {file_path} -' \
  --diagnostics-command 'ruff check --output-format json --stdin-filename {file_path} -'
```

```sh
./simple_lsp_server.py \
  --format-command 'shfmt -filename {file_path} -' \
  --diagnostics-command 'shellcheck - --exclude=SC1091,SC2312 --enable=all --format=json1' \
  --code-actions-command 'shellcheck - --exclude=SC1091,SC2312 --enable=all --format=json1'
```

```sh
./simple_lsp_server.py \
  --format-command 'clang-format --assume-filename={file_path}'
```

```sh
./simple_lsp_server.py \
  --diagnostics-command 'mdl --json {file_path}'
```

```sh
./simple_lsp_server.py \
  --hover-command 'my-hover --file {file_path} --line {line1} --column {character1}' \
  --definition-command 'my-definition --file {file_path} --line {line1} --column {character1}' \
  --references-command 'my-references --file {file_path} --line {line1} --column {character1}' \
  --document-symbols-command 'my-symbols --file {file_path}'
```

Supported diagnostic output formats:

- Ruff JSON from `ruff check --output-format json`
- Markdownlint JSON from `mdl --json`
- ShellCheck JSON from `shellcheck --format=json1`
- GCC-style lines such as `path:line:column: warning: message [CODE]`
- Custom JSON lists with zero-based `line`, `character`, `message`, and
  optional `end_line`, `end_character`, `severity`, `source`, and `code`

Quick-fix code actions currently support ShellCheck JSON from
`shellcheck --format=json1`.

Hover output can be plain Markdown text or JSON:

```json
{"contents": "text", "kind": "markdown"}
```

Definition and reference commands write a JSON location, a location list, or
`null`. Locations use zero-based positions:

```json
{"uri": "file:///tmp/example.py", "line": 0, "character": 0}
```

Document symbols are a JSON list:

```json
[{"name": "main", "kind": "function", "line": 0, "character": 0}]
```
