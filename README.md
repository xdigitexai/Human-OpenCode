# OpenCode Human Bot

A deterministic, **no-AI** coding-automation bot: it indexes source that already
exists, ranks it with fixed lexical rules, and only proposes exact-anchor
replacements derived from a user-selected, licensed source candidate. It does
not generate code from a task description, and every mutation, command,
rollback, screenshot and browser run requires explicit approval.

An independent Python engine plus a VS Code extension. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the design and implemented scope,
[INSTALLATION.md](INSTALLATION.md) for setup, and [SECURITY.md](SECURITY.md)
for the approval boundaries and limitations.

## Components

- `opencode_human_bot/repairs.py` classifies known compiler/linter patterns,
  searches licensed repositories for exact `.opencode-repairs.json` recipes,
  proposes one-file diffs, and requires an approved validation command. A
  failed validation restores the file checkpoint.
- `opencode_human_bot/browser.py` runs approved Playwright scenarios against
  loopback URLs only: structured navigation/form/assertion steps, page
  screenshots, per-step outcomes, and it stops only the process it started.
- `vscode-extension/` ships the sidebar, the native/physical editing modes and
  the `Ctrl+Alt+Escape` emergency stop.

## Editing modes

- **Native mode** edits and saves through the VS Code API.
- **Physical mode** drives the real keyboard and mouse: it pins the exact
  foreground HWND/PID and workspace title, re-checks them before every input,
  asks the extension (or a supervisor) to confirm the active document and
  editor focus, types progressively, saves, verifies the saved bytes against
  the approved plan, and restores the pre-edit checkpoint when anything does
  not match. Focus loss halts input; an interrupted edit is rolled back.

The opt-in physical scenario is a real end-to-end demonstration on a real
window — open, verify, type, save, verify, approved terminal test with a
verified exit code, stage screenshots, editor restore, emergency stop and
checkpoint restore:

```powershell
$env:OPENCODE_RUN_DESKTOP_TEST = "1"
$env:OPENCODE_DESKTOP_TEST_ROOT = "C:\path\to\disposable\folder"
python -m pytest --noconftest tests/test_desktop_physical_windows.py -q -rs
```

## Install

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,codebot]"   # + ".[browser]" and `python -m playwright install chromium` for browser flows
```

The engine's core runs on the Python standard library; the `codebot` extra
adds Tree-sitter parsing and PyAutoGUI for physical mode.

## VS Code extension

```powershell
Push-Location vscode-extension
npm.cmd install
npm.cmd run compile
npm.cmd run package
Pop-Location
code --install-extension .\vscode-extension\opencode-human-bot-0.1.0.vsix
```

Set `opencodeHumanBot.pythonPath` to the environment's Python executable and
choose `native` editing by default; physical mode requires an interactive
desktop and an idle one — it never fights the user's cursor.

## Engine CLI (stdin/stdout JSON)

```
python -m opencode_human_bot --root <workspace>
```

Operations: `inspect`, `index`, `add_local_repository`, `search_github`,
`clone`, `search`, `plan`, `add_replacement`, `apply`, `command`, `rollback`,
`screenshot`, `desktop_start`, `desktop_status`, `desktop_verify_file`,
`desktop_approve_command`, `desktop_cancel`, `propose_repair`,
`apply_repair`, `browser_run`. Approval flags are never implied.
