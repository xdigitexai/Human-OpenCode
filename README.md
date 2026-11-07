# OpenCode Human Bot

A local-first coding workbench with deterministic, approval-gated workflows and
an opt-in AI development agent. The AI agent uses a configured external model,
works only in a disposable local Git branch copy, records file checkpoints and
requires a separate user approval before running a supported validation command.
The original repository is not the AI write target.

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
- `opencode_human_bot/autonomous.py` provides provider adapters, streaming,
  persistent sessions, isolated-copy editing, checkpoints, pause/resume/stop,
  status/reporting, and a constrained approved-test runner.

## Autonomous AI sessions (opt-in)

Select a local repository folder or enter a public HTTPS GitHub URL, configure a
provider key with **API KEY** (stored in VS Code SecretStorage), describe the
task, then press **START**. OpenAI, Anthropic, DeepSeek, Moonshot/Kimi, and
OpenAI-compatible endpoints are supported. The source is copied to a local
`agent/<session-id>` Git branch under `.opencode-human-bot/agent-workspaces/`;
the original folder and remote are not edited. Eligible source/documentation
files are sent to the provider in bounded context snapshots. Environment,
credential, generated/dependency, binary, oversized, and symlinked files are
excluded. The provider receives source data, so choose a provider/privacy policy
appropriate for the repository.

Sessions persist in SQLite and can run up to 24 hours, with a configured pause
between implementation steps. START is explicit; PAUSE/STOP interrupt streamed
responses before any generated changes are applied. Each real file change is
atomic in the isolated copy and creates a recoverable file-content checkpoint.
STATUS shows the current phase and real journal activity; REPORT/REVIEW displays
the unified diff and validation record; ROLLBACK restores only the latest
checkpoint in the isolated workspace. API keys are not written to the session
database. Resuming explicitly sends eligible source context and any saved
validation output to the provider again.

The agent does not automatically run tests or repository scripts. **RUN ISOLATED
TESTS** requires confirmation, runs in the copied workspace with a reduced,
private environment, and permits only recognized local test/lint/compile
commands. Review package test scripts before approval: allowed build/test tools
can still execute repository code. Direct SSH/SCP/SFTP/deployment/production
clients are blocked. There is no network sandbox around a user-approved test
script.

This is a guarded first autonomous implementation, not a full tool-using agent:
provider fallback, model tool-calling, a multi-session task queue, AI-driven error-correction cycles,
physical typing of AI edits, and native VS Code WorkspaceEdit application are
not implemented. AI changes are written into the isolated local copy by the
engine; the existing native/physical workflows remain separate. The model's
self-review is advisory and does not replace a human diff review or tests.

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
`apply_repair`, `browser_run`, `agent_start`, `agent_status`, `agent_pause`,
`agent_resume`/`agent_restart`, `agent_stop`, `agent_report`, `agent_diff`,
`agent_rollback`, `agent_validate`. Approval flags are never implied.
