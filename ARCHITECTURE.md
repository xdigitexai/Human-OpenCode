# OpenCode Human Bot Architecture

OpenCode Human Bot is an independent Python engine and VS Code extension in this workspace. The existing MT5 application remains separate. Existing deterministic workflows index source and propose licensed exact-anchor replacements. An additional opt-in AI worker can generate and write code, but only in a disposable local Git branch copy. Provider calls transfer eligible source context to the selected external endpoint.

## Components

- `opencode_human_bot/repairs.py` classifies known compiler/linter patterns, searches licensed repositories for exact `.opencode-repairs.json` recipes, proposes one-file diffs, and requires an approved validation command. A failed validation restores the file checkpoint.
- `opencode_human_bot/browser.py` runs approved Playwright scenarios against loopback URLs only. It can start a local application command, perform structured navigation/form/assertion steps, capture page screenshots, report per-step outcomes, and stop only the process it started.
- `opencode_human_bot/autonomous.py` provides provider adapters, persistent sessions, isolated-copy editing, checkpoints, pause/resume/stop, reporting, rollback, token accounting, and an approval-gated local validation allowlist.

The default database and cloned repositories live in `.opencode-human-bot/`, which is ignored by Git. `rg` can be used by an operator from the terminal; indexed symbol search itself uses SQLite so results carry repository and license attribution.

## Request flow

1. Inspect the active workspace and detect source files, likely frameworks, dependency manifests, parser availability, and compiler/test executable availability.
2. Index the active project or an explicitly selected local/public GitHub repository. Tree-sitter extracts function/class structures; import names and manifest paths are recorded.
3. Rank indexed structures using exact symbol, path, and body term matches. Candidates preserve source path, repository, URL, and license text.
4. Require a license file and same-language match before a candidate can enter a task plan. Dependency/ABI version compatibility is surfaced as unverified, never guessed.
5. A user selects a candidate and supplies the exact old and replacement text. The old text must occur once in the existing target file. The engine displays a unified diff and records attribution.
6. On approval, snapshot target files and pin the current Git `HEAD` under `refs/opencode-human-bot/<checkpoint>`. Native mode edits and saves through VS Code; physical mode uses keyboard automation.
7. Test commands, clone, rollback, and screenshot actions require separate user approval. Results and action summaries are persisted locally.

## Autonomous session flow

1. The user selects a local folder or approves cloning a public HTTPS GitHub repository, chooses a provider, configures the provider key through VS Code SecretStorage, and explicitly starts a task.
2. The engine validates the provider endpoint before copying data, excludes symlinks, known credentials/environment files, dependency/build folders, binaries, and oversized files, then initializes an isolated local Git branch under `.opencode-human-bot/agent-workspaces/`.
3. A background worker reads bounded context from the isolated copy and makes provider requests. The model has no shell or direct tool execution. Streaming callbacks publish limited live response text and check PAUSE/STOP before any output is applied.
4. Generated file contents must be within the isolated workspace and match the exact snapshot read by that model pass; protected paths and unsafe credential-like content are rejected. The engine writes the complete multi-file step transactionally and records before-images plus unified diffs in SQLite. Identical file contents are not counted as progress.
5. Session summaries, next task, phases, token counters, deadline, checkpoints, validation output, and a bounded journal survive engine restarts. An interrupted worker requires an explicit resume with provider credentials supplied again from SecretStorage.
6. Validation is never automatic. After a session is paused or terminal, the host confirms a recognized command; the worker runs it in the isolated directory with a private HOME and reduced environment. A failed result is journaled and changes the session to paused for an explicit user-directed resume.

The autonomous manager never receives model-provided shell calls, SSH, deployment, or push permissions. Approved package test scripts remain repository code and can have side effects; command allowlisting is not an operating-system network sandbox.

## Persistence

SQLite tables are initialized on startup and require no server. A checkpoint stores the prior content of affected files, plus the Git commit ref when the project has a commit. Rollback restores only files recorded in that checkpoint; it does not reset or move the user's branch.

## Implemented status

- **M1, working engine and extension:** implemented; Python workflow tests pass and the extension compiles. The extension has not yet been exercised in a live Extension Development Host.
- **M2, repository discovery/indexing:** local-folder intake, unauthenticated GitHub repository search, approval-gated HTTPS clone, license capture, and SQLite indexing are implemented. GitHub API rate limits apply.
- **M3, language adapters:** all 13 parser mappings pass representative Tree-sitter parse tests with `tree-sitter-language-pack` installed. Import scanning and manifest discovery are heuristic; adapters do not yet provide exhaustive version resolution or full dependency graphs.
- **M4, deterministic adaptation:** unique-anchor replacement with a human-supplied replacement and attribution is implemented separately from the autonomous AI mode.
- **M5, editor interaction:** VS Code API mode opens, edits, and saves documents. Physical mode uses the persistent worker and PyAutoGUI, pins the exact foreground HWND/PID and workspace title, confirms the opened file and editor focus, progressively types, saves and verifies, scrolls, and captures stage screenshots. The extension can open a selected folder in a new window using the VS Code API, without replacing the current workspace.
- **M6, verification/recovery:** approved command execution, test output capture, Git/file checkpoints, explicit rollback, known diagnostic classification, licensed exact repair recipes, validation rollback, Playwright local scenarios, screenshots, and a global `Ctrl+Alt+Escape` emergency stop are implemented. Unknown diagnostics are not repaired and no fixes are generated.
- **M7, autonomous isolated AI sessions:** provider adapters and streaming, secure key delivery from VS Code SecretStorage, local/GitHub source intake, durable background sessions, context limits, conflict-checked multi-file edits, local branch/checkpoint state, configurable inter-step review breaks, user-controlled pause/stop/resume/restart, status/report/diff/rollback, token usage, and explicitly approved constrained validation are implemented.
- **Known gaps:** a multi-session task queue, automatic provider fallback, model tool calling, automatic AI repair iterations, a separate AI planning/reviewer round, physical typing of AI-generated edits, and VS Code `WorkspaceEdit` application for AI sessions are not implemented. A user may explicitly resume after a failed test; its saved output is then included in the provider context. Existing native/physical workflows remain separate. Safeguards have automated tests, but a live VS Code Extension Development Host has not been exercised here.

The current tests exercise engine behaviors, parser grammar samples, desktop task progression with a mocked OS, repair validation/rollback, and a real Playwright form flow. In this environment the top-level `Code.exe` rejects CLI flags and `bin/code.cmd` has not reliably created a separate window; the opt-in physical test therefore skips before mouse/keyboard input. The extension's `Open Folder in New Window` command uses VS Code's supported `vscode.openFolder(..., { forceNewWindow: true })` API and is the preferred attachment path. The suite does not prove that every compiler is installed, every dependency graph is compatible, or physical interaction works on other desktop configurations.
