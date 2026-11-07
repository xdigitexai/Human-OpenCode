from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from opencode_human_bot.desktop import DesktopController, DesktopError
from opencode_human_bot.engine import CodingEngine


@contextmanager
def _desktop_project():
    configured = os.environ.get("OPENCODE_DESKTOP_TEST_ROOT")
    if configured:
        project = Path(configured).resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if temporary_root not in project.parents or not project.is_dir():
            raise ValueError("OPENCODE_DESKTOP_TEST_ROOT must be an existing disposable folder under the system temp directory")
        yield project, project.parent
        return
    with tempfile.TemporaryDirectory(prefix="OpenCodeHumanBot-Desktop-") as temporary:
        base = Path(temporary)
        project = base / "OpenCodeHumanBot-DesktopFixture"
        yield project, base


def _vscode_cli() -> Path | None:
    configured = os.environ.get("VSCODE_CLI")
    candidates = [Path(configured)] if configured else []
    candidates.extend([
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Microsoft VS Code/bin/code.cmd",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft VS Code/bin/code.cmd",
    ])
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def _activate_window(root_name: str, deadline: float) -> Any | None:
    pyautogui = DesktopController._pyautogui()
    trading_windows = [window for window in pyautogui.getAllWindows()
                       if "MT5-Trading-Bot-" in (window.title or "")]
    trading_handles = {int(window._hWnd) for window in trading_windows}
    while time.monotonic() < deadline:
        windows = [window for window in pyautogui.getAllWindows()
                   if root_name.casefold() in (window.title or "").casefold()]
        for window in windows:
            if int(window._hWnd) in trading_handles:
                continue
            if window.width < 100 or window.height < 100:
                continue
            if window.isMinimized:
                window.restore()
            # Foreground lock can reject SetForegroundWindow from a background process; the click below retries.
            with contextlib.suppress(Exception):
                window.activate()
            time.sleep(0.25)
            if int(window._hWnd) != int(DesktopController._foreground_identity()["hwnd"]):
                pyautogui.click(max(window.left + 250, 20), max(window.top + 12, 12))
                time.sleep(0.25)
            if (DesktopController._is_vscode_process(DesktopController._foreground_process())
                    and int(window._hWnd) == int(DesktopController._foreground_identity()["hwnd"])
                    and root_name.casefold() in DesktopController._foreground_title().casefold()):
                return window
        time.sleep(0.2)
    return None


def _wait_for_task(engine: CodingEngine, task_id: str, root_name: str,
                   timeout: float, stop_while_typing: bool = False,
                   terminal_command: str | None = None,
                   terminal_output: Path | None = None,
                   screenshot_path: Path | None = None,
                   expected_hwnd: int | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    stop_requested = False
    terminal_approved = False
    command_typed = False
    verify_seen_at: float | None = None
    previous_status: str | None = None
    while time.monotonic() < deadline:
        task = engine.desktop.status(task_id)
        if task["status"] != previous_status:
            verify_seen_at = None
            previous_status = task["status"]
        if task["status"] in {"verify_file", "verify_editor_focus"}:
            try:
                title = DesktopController._foreground_title().casefold()
                process = DesktopController._foreground_process()
                identity = DesktopController._foreground_identity()
            except DesktopError:
                # Transient foreground transition; the grace window below decides.
                title, process, identity = "", "unknown", {"hwnd": None}
            verified = bool(
                DesktopController._is_vscode_process(process) and root_name.casefold() in title
                and (task["current_path"] in title or Path(task["current_path"]).name in title)
                and (expected_hwnd is None or identity["hwnd"] == expected_hwnd))
            if verified:
                engine.desktop.acknowledge_file(task_id, True)
            # Mirror the extension's verifier: confirm only when the checks genuinely pass.
            # A short grace window tolerates VS Code title lag; persistent failure still rejects.
            elif verify_seen_at is None:
                verify_seen_at = time.monotonic()
            elif time.monotonic() - verify_seen_at > 4.0:
                engine.desktop.acknowledge_file(task_id, False)
        elif task["status"] == "awaiting_command_approval" and terminal_command and not terminal_approved:
            engine.desktop.approve_command(task_id, True)
            terminal_approved = True
        elif task["status"] == "command_ready" and terminal_command and not command_typed:
            def foreground_matches() -> bool | None:
                try:
                    return (expected_hwnd is None
                            or DesktopController._foreground_identity()["hwnd"] == expected_hwnd)
                except DesktopError:
                    return None  # transient foreground transition; retry the same check

            deadline_checks = time.monotonic() + 1.0
            while time.monotonic() < deadline_checks:
                state = foreground_matches()
                if state is not None:
                    break
                time.sleep(0.05)
            if not state:
                engine.desktop.cancel(task_id)
                pytest.fail("Foreground HWND changed before approved terminal input")
            automation = DesktopController._pyautogui()
            for character in terminal_command:
                char_deadline = time.monotonic() + 1.0
                while expected_hwnd is not None and not foreground_matches():
                    if time.monotonic() > char_deadline:
                        engine.desktop.cancel(task_id)
                        pytest.fail("Foreground moved during approved terminal typing")
                    time.sleep(0.05)
                automation.write(character, interval=0)
                time.sleep(0.02)
            tail_deadline = time.monotonic() + 1.0
            while time.monotonic() < tail_deadline:
                state = foreground_matches()
                if state is not None:
                    break
                time.sleep(0.05)
            if not state:
                engine.desktop.cancel(task_id)
                pytest.fail("Focus moved before terminal command submission")
            automation.press("enter")
            command_typed = True
        if command_typed and terminal_output and terminal_output.with_suffix(".exit").is_file():
            exit_file = terminal_output.with_suffix(".exit")
            output = _read_output_text(terminal_output) if terminal_output.exists() else ""
            exit_code = int(_read_output_text(exit_file).strip())
            if screenshot_path:
                DesktopController._pyautogui().screenshot().save(screenshot_path)
            engine.desktop.report_command(task_id, {
                "verified": True, "exit_code": exit_code, "output": output,
                "evidence": "disposable terminal output and exit-code files",
            })
            terminal_output = None
        elif (stop_while_typing and not stop_requested and task["action"].startswith("Typing approved changes")):
            engine.desktop.cancel(task_id)
            stop_requested = True
        if task["status"] in {"completed", "cancelled", "blocked", "tests_failed", "interrupted"}:
            task["stop_requested_by_test"] = stop_requested
            return task
        time.sleep(0.05)
    engine.desktop.cancel(task_id)
    pytest.fail(f"Desktop task {task_id} did not finish in time; last status: {engine.desktop.status(task_id)}")


def _read_output_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16")
    return raw.decode("utf-8", errors="replace")


def _window_process_id(window: Any) -> int | None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    process_id = ctypes.c_ulong()
    if not user32.GetWindowThreadProcessId(window._hWnd, ctypes.byref(process_id)):
        return None
    return process_id.value


@pytest.mark.skipif(os.name != "nt", reason="Physical desktop verification is Windows-only")
@pytest.mark.skipif(os.environ.get("OPENCODE_RUN_DESKTOP_TEST") != "1",
                    reason="Set OPENCODE_RUN_DESKTOP_TEST=1 to launch an isolated VS Code window and use the real mouse/keyboard")
def test_real_vscode_physical_edit_save_screenshot_and_emergency_stop():
    cli = _vscode_cli()
    if cli is None:
        pytest.skip("VS Code CLI was not found; set VSCODE_CLI to the full path of bin/code.cmd")
    pyautogui = DesktopController._pyautogui()

    with _desktop_project() as (project, base):
        library = project / "library"
        profile = base / "profile"
        extensions = base / "extensions"
        library.mkdir(parents=True, exist_ok=True)
        target = project / "target.py"
        target.write_text("def answer():\n    return 40\n", encoding="utf-8")
        (library / "answer.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        (library / "LICENSE").write_text("MIT License\nPermission granted for this disposable fixture.\n", encoding="utf-8")
        vscode_process = None
        engine: CodingEngine | None = None
        window = None
        try:
            window = _activate_window(project.name, time.monotonic() + 45)
            if window is None:
                vscode_process = subprocess.Popen([
                    "cmd.exe", "/d", "/c", str(cli), "--new-window", "--disable-workspace-trust",
                    f"--user-data-dir={profile}", f"--extensions-dir={extensions}", str(project),
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                window = _activate_window(project.name, time.monotonic() + 120)
            if window is None:
                pytest.skip("VS Code CLI did not expose a matching foreground disposable workspace; no input was sent")
            trading_window = next((candidate for candidate in pyautogui.getAllWindows()
                                   if "MT5-Trading-Bot-" in (candidate.title or "")), None)
            if trading_window is not None and trading_window._hWnd == window._hWnd:
                pytest.fail("Refusing physical input: disposable window is the trading workspace HWND")
            expected_hwnd = int(window._hWnd)
            cursor_before = pyautogui.position()
            engine = CodingEngine(project, base / "engine.sqlite3")
            source_repo = engine.add_local_repository(library)
            engine.index_repository(library)
            engine.index_repository(project)

            plan = engine.create_plan("answer Python")
            assert plan["candidates"], "No licensed local source candidate was found"
            final_source = "def answer():\n    return 42\n\nif __name__ == \"__main__\":\n    print(answer())\n"
            plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return 40", "return 42\n\nif __name__ == \"__main__\":\n    print(answer())")
            terminal_output = project / ".desktop-test-output.txt"
            terminal_exit = project / ".desktop-test-output.exit"
            for generated in (terminal_output, terminal_exit):
                with contextlib.suppress(FileNotFoundError):
                    generated.unlink()
            terminal_command = (
                f'& "{sys.executable}" "target.py" | Tee-Object -FilePath "{terminal_output.name}"; '
                f'$code=$LASTEXITCODE; Set-Content -Path "{terminal_exit.name}" -Value "$code" -NoNewline'
            )
            task = engine.desktop.start(plan["id"], {
                "typing_interval": 0.015,
                "mouse_duration": 0.15,
                "action_delay": 3.0,
                "capture_screenshots": True,
                "cursor_line": 2,
                "explorer_items": {"target.py": [[38, 84], [145, 227]]},
            }, command=[sys.executable, "target.py"], approved=True)
            completed = _wait_for_task(engine, task["id"], project.name, timeout=90,
                                       terminal_command=terminal_command, terminal_output=terminal_output,
                                       screenshot_path=project / "desktop-terminal-output.png",
                                       expected_hwnd=expected_hwnd)
            assert completed["status"] == "completed", completed
            assert target.read_text(encoding="utf-8") == final_source
            assert _read_output_text(terminal_output).strip() == "42"
            assert terminal_exit.read_text(encoding="utf-8").strip() == "0"
            assert pyautogui.position() != cursor_before, "The OS cursor did not move during the task"
            screenshots = completed["screenshots"]
            assert screenshots, "The physical task did not capture a stage screenshot"
            assert all((project / item).is_file() and (project / item).stat().st_size > 0 for item in screenshots)

            long_replacement = "return 42\n" + "# emergency-stop-fixture\n" * 1200
            stop_plan = engine.create_plan("answer Python")
            stop_plan = engine.add_exact_replacement(
                stop_plan["id"], 0, "target.py", "return 42", long_replacement.rstrip("\n"))
            stop_task = engine.desktop.start(stop_plan["id"], {
                "typing_interval": 0.02,
                "mouse_duration": 0.1,
                "action_delay": 0.1,
                "capture_screenshots": False,
                "explorer_items": {"target.py": [[38, 84], [145, 227]]},
            }, approved=True)
            stopped = _wait_for_task(engine, stop_task["id"], project.name,
                                     timeout=60, stop_while_typing=True, expected_hwnd=expected_hwnd)
            assert stopped["status"] == "cancelled", stopped
            assert stopped["stop_requested_by_test"] is True
            assert target.read_text(encoding="utf-8") == final_source
            assert source_repo["licensed"]
        finally:
            if engine is not None:
                engine.close()
            if window is not None:
                with contextlib.suppress(OSError):
                    title = window.title or ""
                    if project.name.casefold() in title.casefold():
                        user32 = ctypes.WinDLL("user32", use_last_error=True)
                        user32.PostMessageW(window._hWnd, 0x0010, 0, 0)
            if vscode_process is not None:
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    if vscode_process.poll() is None:
                        vscode_process.terminate()
                        vscode_process.wait(timeout=5)