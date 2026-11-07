# OpenCode Human Bot Architecture

OpenCode Human Bot is an independent Python engine and VS Code extension in this workspace. The existing MT5 application remains separate. Runtime behavior is deterministic: the engine indexes source that already exists, ranks it with fixed lexical rules, and only proposes exact-anchor replacements derived from a user-selected, licensed source candidate. It does not generate code from a task description.

## Components

- `opencode_human_bot/repairs.py` classifies known compiler/linter patterns, searches licensed repositories for exact `.opencode-repairs.json` recipes, proposes one-file diffs, and requires an approved validation command. A failed validation restores the file checkpoint.
- `opencode_human_bot/browser.py` runs approved Playwright scenarios against loopback URLs only. It can start a local application command, perform structured navigation/form/assertion steps, capture page screenshots, report per-step outcomes, and stop only the process it started.

The default database and cloned repositories live in `.opencode-human-bot/`, which is ignored by Git. `rg` can be used by an operator from the terminal; indexed symbol search itself uses SQLite so results carry repository and license attribution.

## Request flow

1. Inspect the active workspace and detect source files, likely frameworks, dependency manifests, parser availability, and compiler/test executable availability.
2. Index the active project or an explicitly selected local/public GitHub repository. Tree-sitter extracts function/class structures; import names and manifest paths are recorded.
3. Rank indexed structures using exact symbol, path, and body term matches. Candidates preserve source path, repository, URL, and license text.
4. Require a license file and same-language match before a candidate can enter a task plan. Dependency/ABI version compatibility is surfaced as unverified, never guessed.
5. A user selects a candidate and supplies the exact old and replacement text. The old text must occur once in the existing target file. The engine displays a unified diff and records attribution.
6. On approval, snapshot target files and pin the current Git `HEAD` under `refs/opencode-human-bot/<checkpoint>`. Native mode edits and saves through VS Code; physical mode uses keyboard automation.
7. Test commands, clone, rollback, and screenshot actions require separate user approval. Results and action summaries are persisted locally.

## Persistence

SQLite tables are initialized on startup and require no server. A checkpoint stores the prior content of affected files, plus the Git commit ref when the project has a commit. Rollback restores only files recorded in that checkpoint; it does not reset or move the user's branch.

## Implemented status

- **M1, working engine and extension:** implemented; Python workflow tests pass and the extension compiles. The extension has not yet been exercised in a live Extension Development Host.
- **M2, repository discovery/indexing:** local-folder intake, unauthenticated GitHub repository search, approval-gated HTTPS clone, license capture, and SQLite indexing are implemented. GitHub API rate limits apply.
- **M3, language adapters:** all 13 parser mappings pass representative Tree-sitter parse tests with `tree-sitter-language-pack` installed. Import scanning and manifest discovery are heuristic; adapters do not yet provide exhaustive version resolution or full dependency graphs.
- **M4, deterministic adaptation:** unique-anchor replacement with a human-supplied replacement and attribution is implemented. There is no autonomous code synthesis, multi-file recipe engine, or general-purpose migration system.
- **M5, editor interaction:** VS Code API mode opens, edits, and saves documents. Physical mode uses the persistent worker and PyAutoGUI, pins the exact foreground HWND/PID and workspace title, confirms the opened file and editor focus, progressively types, saves and verifies, scrolls, and captures stage screenshots. The extension can open a selected folder in a new window using the VS Code API, without replacing the current workspace.
- **M6, verification/recovery:** approved command execution, test output capture, Git/file checkpoints, explicit rollback, known diagnostic classification, licensed exact repair recipes, validation rollback, Playwright local scenarios, screenshots, and a global `Ctrl+Alt+Escape` emergency stop are implemented. Unknown diagnostics are not repaired and no fixes are generated.

The current tests exercise engine behaviors, parser grammar samples, desktop task progression with a mocked OS, repair validation/rollback, and a real Playwright form flow. In this environment the top-level `Code.exe` rejects CLI flags and `bin/code.cmd` has not reliably created a separate window; the opt-in physical test therefore skips before mouse/keyboard input. The extension's `Open Folder in New Window` command uses VS Code's supported `vscode.openFolder(..., { forceNewWindow: true })` API and is the preferred attachment path. The suite does not prove that every compiler is installed, every dependency graph is compatible, or physical interaction works on other desktop configurations.