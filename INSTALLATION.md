# Installation

## Requirements

- Python 3.11 or newer.
- Git on `PATH` for Git checkpoints and repository cloning.
- Visual Studio Code 1.90 or newer for the sidebar extension.
- Node.js and npm for building the extension.
- Optional: `rg` for standalone fast text search; PyAutoGUI and a logged-in desktop session for physical mode.

## Python engine

From the workspace root, create and activate a virtual environment, then install the development and coding-bot extras:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,codebot]"
```

The `codebot` extra installs Tree-sitter language packs and PyAutoGUI. The engine's inspection and approval workflow use the Python standard library; without the parser extra, the UI reports that parsing is unavailable and cannot index syntax structures.

For browser testing, install the optional Playwright extra and its Chromium runtime:

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

## VS Code extension

```powershell
Push-Location vscode-extension
npm.cmd install
npm.cmd run compile
npm.cmd run package
Pop-Location
code --install-extension .\vscode-extension\opencode-human-bot-0.1.0.vsix
```

Set `opencodeHumanBot.pythonPath` to the virtual environment's Python executable if `python` on the extension host's `PATH` is not the environment where the package was installed. Choose `native` editing by default. Physical mode requires an interactive desktop and should only be enabled when VS Code is the foreground application.

Physical mode settings are under `opencodeHumanBot.desktop`: `typingInterval`, `mouseDuration`, and `actionDelay` control pacing; `coordinates` can override Explorer/editor/terminal positions and map a relative path to Explorer click points, e.g. `{ "explorer_items": { "src/main.py": [[38, 84], [140, 160], [155, 225]] } }`; `cursor_line` moves the caret to a line before typing; `captureScreenshots` controls staged desktop captures. The emergency-stop button and `Ctrl+Alt+Escape` shortcut stop physical work. Focus loss halts keyboard input and restores the checkpoint when possible.

For a disposable physical test, open the Command Palette and run **OpenCode Human Bot: Open Folder in New Window**, then select the test project. This uses VS Code's `vscode.openFolder` command with `forceNewWindow: true`; it does not replace the current trading workspace.

Open a project folder in VS Code, select the OpenCode Human Bot activity-bar icon, and use **Index workspace source** before searching or proposing work. To use the extension while developing it, open `vscode-extension` as a VS Code folder and launch an Extension Development Host after adding a local launch configuration.

## Validation

The focused engine tests in this repository can be run without loading the MT5 project's global fixtures:

```powershell
python -m pytest --noconftest tests/test_opencode_human_bot.py -q
```

Compile the extension with `npm.cmd --prefix vscode-extension run compile`. Approved test commands are executed directly as argument arrays, not through a shell.

The real physical desktop test is opt-in and uses a disposable project/profile:

```powershell
$env:OPENCODE_RUN_DESKTOP_TEST = "1"
python -m pytest --noconftest tests/test_desktop_physical_windows.py -q -rs
```

Install/reload the VSIX first, then in the active VS Code run **OpenCode Human Bot: Open Folder in New Window** and select the disposable folder. Set `OPENCODE_DESKTOP_TEST_ROOT` to that folder before running the test. The test requires a matching foreground HWND and a distinct window from the trading workspace; if either check fails, it skips before mouse or keyboard input. Do not substitute the active project window when unsaved user work is present. The external `bin/code.cmd` launcher is a fallback only; this host has shown it exiting or hanging without opening a matching window.