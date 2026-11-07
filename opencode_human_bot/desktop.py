from __future__ import annotations

import ctypes
import importlib
import json
import os
import threading
import time
import uuid
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DesktopError(RuntimeError):
    pass


class DesktopCancelled(DesktopError):
    pass


class DesktopController:
    _columns: ClassVar[set[str]] = {"status", "step", "total", "action", "current_path", "payload", "checkpoint",
                                   "file_verified", "command_approved", "command_result", "stop_requested", "error"}

    def __init__(self, engine: Any):
        self.engine = engine
        self.root: Path = engine.root
        self.store = engine.store
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._thread: threading.Thread | None = None
        self._recover_stale_tasks()

    def _recover_stale_tasks(self) -> None:
        rows = self.store.execute(
            "SELECT id FROM desktop_tasks WHERE status IN "
            "('queued','running','verify_file','verify_editor_focus','awaiting_command_approval',"
            "'command_ready','command_result') ORDER BY created_at"
        ).fetchall()
        for row in rows:
            self._recover_task(row["id"], "interrupted", "Engine restarted before desktop task completion")

    @staticmethod
    def _foreground_process() -> str:
        if os.name != "nt":
            raise DesktopError("Physical desktop control is supported only on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise DesktopError("Could not identify the foreground window")
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        handle = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not handle:
            raise DesktopError("Could not verify the foreground application")
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                raise DesktopError("Could not verify the foreground application")
            return Path(buffer.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)
    @staticmethod
    def _is_vscode_process(process_name: str) -> bool:
        return process_name.lower() in {"code.exe", "code - insiders.exe", "code - exploration.exe"}

    @staticmethod
    def _foreground_title() -> str:
        if os.name != "nt":
            raise DesktopError("Physical desktop control is supported only on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise DesktopError("Could not identify the foreground window")
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        title = ctypes.create_unicode_buffer(1024)
        user32.GetWindowTextW(hwnd, title, len(title))
        return title.value

    @staticmethod
    def _foreground_identity() -> dict[str, int]:
        if os.name != "nt":
            raise DesktopError("Physical desktop control is supported only on Windows")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            raise DesktopError("Could not identify the foreground window")
        process_id = wintypes.DWORD()
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id)):
            raise DesktopError("Could not identify the foreground window process")
        return {"hwnd": int(hwnd), "pid": int(process_id.value)}

    def _is_target_window(self) -> bool:
        return self.root.name.casefold() in self._foreground_title().casefold()

    @staticmethod
    def _pyautogui():
        try:
            automation = importlib.import_module("pyautogui")
        except ImportError as error:
            raise DesktopError("Physical mode requires the optional PyAutoGUI dependency") from error
        automation.FAILSAFE = True
        return automation

    @classmethod
    def _validate_settings(cls, settings: dict[str, Any], screen: tuple[int, int]) -> dict[str, Any]:
        width, height = screen
        if width < 100 or height < 100:
            raise ValueError("Desktop screen dimensions are unusable")
        defaults = {"explorer_x": 38, "explorer_y": min(84, height - 1),
                    "editor_x": width // 2, "editor_y": height // 3,
                    "terminal_x": width // 2, "terminal_y": max(0, height - 140),
                    "typing_interval": 0.035, "mouse_duration": 0.25, "action_delay": 0.6,
                    "focus_retry_seconds": 1.5}
        result = dict(defaults)
        for key in defaults:
            if key in settings:
                result[key] = settings[key]
        for key in ("explorer_x", "explorer_y", "editor_x", "editor_y", "terminal_x", "terminal_y"):
            value = result[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{key} must be an integer screen coordinate")
        for key, maximum in (("typing_interval", 0.5), ("mouse_duration", 3.0),
                     ("action_delay", 5.0), ("focus_retry_seconds", 10.0)):
            value = result[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= maximum:
                raise ValueError(f"{key} must be between 0 and {maximum}")
            result[key] = float(value)
        for prefix in ("explorer", "editor", "terminal"):
            if not 0 <= result[f"{prefix}_x"] < width or not 0 <= result[f"{prefix}_y"] < height:
                raise ValueError(f"{prefix} mouse coordinates fall outside the screen")
        result["capture_screenshots"] = settings.get("capture_screenshots") is True
        result["explorer_items"] = settings.get("explorer_items", {})
        if not isinstance(result["explorer_items"], dict):
            raise TypeError("explorer_items must map workspace-relative paths to coordinate lists")
        for relative, points in result["explorer_items"].items():
            if not isinstance(relative, str) or not isinstance(points, list) or not points:
                raise ValueError("Each Explorer path must have a non-empty list of screen coordinates")
            for point in points:
                if (not isinstance(point, (list, tuple)) or len(point) != 2 or
                        any(not isinstance(value, int) or isinstance(value, bool) for value in point)):
                    raise TypeError("Explorer click points must be integer [x, y] pairs")
                if not 0 <= point[0] < width or not 0 <= point[1] < height:
                    raise ValueError("Explorer click point falls outside the screen")
        result["cursor_line"] = settings.get("cursor_line")
        if result["cursor_line"] is not None and (not isinstance(result["cursor_line"], int) or
                                                    isinstance(result["cursor_line"], bool) or
                                                    result["cursor_line"] < 1):
            raise ValueError("cursor_line must be a positive integer")
        return result

    def start(self, plan_id: str, settings: dict[str, Any], command: list[str] | None = None,
              approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Starting desktop operation requires explicit approval")
        automation = self._pyautogui()
        if not self._is_vscode_process(self._foreground_process()):
            raise DesktopError("VS Code must be the foreground application before desktop operation starts")
        if not self._is_target_window():
            raise DesktopError("The foreground VS Code window does not match the active workspace")
        window_identity = self._foreground_identity()
        config = self._validate_settings(settings, automation.size())
        if command is not None and (not isinstance(command, list) or not command or
                                    any(not isinstance(part, str) or "\x00" in part for part in command)):
            raise ValueError("The optional test command must be a non-empty argument array")
        plan = self.engine.get_plan(plan_id)
        if not plan["changes"]:
            raise ValueError("The plan contains no approved edit proposals")
        prepared = self.engine.prepare_apply(plan_id, approved=True)
        task_id = uuid.uuid4().hex
        payload = {"changes": prepared["changes"], "settings": config, "command": command,
                   "completed_files": [], "screenshots": [], "window_identity": window_identity}
        try:
            self.store.execute(
                "INSERT INTO desktop_tasks(id,plan_id,status,total,payload,checkpoint,created_at,updated_at) "
                "VALUES(?,?,'queued',?,?,?,?,?)",
                (task_id, plan_id, len(payload["changes"]), self.store.encode(payload),
                 self.store.encode(prepared["checkpoint"]), _now(), _now()))
            self.store.commit()
        except Exception:
            self.engine.abort_apply(plan_id)
            raise
        self._start_worker()
        self._log(f"Queued approved physical task {task_id}")
        self._wake.set()
        return self.status(task_id)

    def _start_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._worker, name="OpenCodeHumanBotDesktop", daemon=True)
            self._thread.start()

    def _log(self, message: str) -> None:
        self.engine._log("desktop", message)

    def _set(self, task_id: str, **fields: Any) -> None:
        if not fields or set(fields) - self._columns:
            raise ValueError("Invalid desktop task update")
        values = {key: self.store.encode(value) if key in {"payload", "checkpoint", "command_result"} else value
                  for key, value in fields.items()}
        assignments = ",".join(f"{key}=?" for key in values)
        self.store.execute(f"UPDATE desktop_tasks SET {assignments},updated_at=? WHERE id=?",
                           (*values.values(), _now(), task_id))
        self.store.commit()
        self._wake.set()

    def status(self, task_id: str | None = None, limit: int = 20) -> Any:
        if task_id:
            row = self.store.execute("SELECT * FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
            return self._public_row(row) if row else None
        rows = self.store.execute("SELECT * FROM desktop_tasks ORDER BY created_at DESC LIMIT ?",
                                  (min(max(limit, 1), 100),)).fetchall()
        return [self._public_row(row) for row in rows]

    @staticmethod
    def _public_row(row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        payload = json.loads(row["payload"])
        return {"id": row["id"], "plan_id": row["plan_id"], "status": row["status"],
                "step": row["step"], "total": row["total"], "action": row["action"],
                "current_path": row["current_path"], "command": payload.get("command"),
                "completed_files": payload.get("completed_files", []),
                "screenshots": payload.get("screenshots", []),
                "checkpoint": json.loads(row["checkpoint"]), "error": row["error"],
                "command_result": json.loads(row["command_result"]) if row["command_result"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def acknowledge_file(self, task_id: str, verified: bool) -> dict[str, Any]:
        row = self.store.execute("SELECT status FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] not in {"verify_file", "verify_editor_focus"}:
            raise ValueError("Task is not waiting for editor verification")
        self._set(task_id, file_verified=int(verified),
                  status="running" if verified else "blocked",
                  error=None if verified else "VS Code could not confirm the requested file and editor focus")
        return self.status(task_id)

    def approve_command(self, task_id: str, approved: bool) -> dict[str, Any]:
        row = self.store.execute("SELECT status FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != "awaiting_command_approval":
            raise ValueError("Task is not waiting for terminal approval")
        self._set(task_id, command_approved=int(approved), status="command_ready" if approved else "approval_denied")
        return self.status(task_id)

    def report_command(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        row = self.store.execute("SELECT status FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["status"] != "command_ready":
            raise ValueError("Task is not waiting for terminal execution")
        if result.get("verified") is not True:
            result["exit_code"] = None
        self._set(task_id, command_result=result, status="command_result")
        return self.status(task_id)

    def cancel(self, task_id: str | None = None) -> dict[str, Any]:
        query = "UPDATE desktop_tasks SET stop_requested=1,updated_at=? WHERE status IN " \
            "('queued','running','verify_file','verify_editor_focus','awaiting_command_approval','command_ready')"
        parameters: tuple[Any, ...] = (_now(),)
        if task_id:
            query += " AND id=?"
            parameters += (task_id,)
        cursor = self.store.execute(query, parameters)
        self.store.commit()
        self._wake.set()
        return {"requested": cursor.rowcount, "task_id": task_id}

    def close(self) -> None:
        self._closed = True
        self.cancel()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join()

    def _worker(self) -> None:
        while not self._closed:
            row = self.store.execute("SELECT * FROM desktop_tasks WHERE status='queued' "
                                     "ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            try:
                self._execute(row["id"])
            except DesktopCancelled as error:
                self._recover_task(row["id"], "cancelled", str(error))
            except Exception as error:  # noqa: BLE001
                self._recover_task(row["id"], "blocked", str(error))

    def _check(self, task_id: str, automation: Any) -> None:
        if self._closed:
            raise DesktopCancelled("Desktop controller is shutting down")
        row = self.store.execute("SELECT stop_requested,payload FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None or row["stop_requested"]:
            raise DesktopCancelled("Emergency stop or cancellation requested")
        payload = json.loads(row["payload"])
        expected = payload.get("window_identity")
        last_actual: dict[str, int] | None = None
        last_process = "unknown"
        last_title = "unknown"
        retries = int(payload.get("settings", {}).get("focus_retry_seconds", 1.5) / 0.1)
        for attempt in range(retries + 1):
            row = self.store.execute("SELECT stop_requested FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["stop_requested"] or self._closed:
                raise DesktopCancelled("Emergency stop or cancellation requested")
            try:
                last_actual = self._foreground_identity()
                last_process = self._foreground_process()
                last_title = self._foreground_title()
            except DesktopError:
                # A transient foreground transition can return no window; treat it as a mismatch
                # so the retry window decides, never a hard crash mid-task.
                last_actual, last_process, last_title = None, "unknown", "unknown"
            if ((not expected or last_actual == expected) and self._is_vscode_process(last_process)
                    and self.root.name.casefold() in last_title.casefold()):
                return
            if attempt < retries:
                time.sleep(0.1)
        raise DesktopError(f"Focus did not return to the exact VS Code window; expected={expected}, "
                   f"actual={last_actual}, process={last_process}, title={last_title!r}; input stopped")

    def _wait_for(self, task_id: str, column: str) -> Any:
        while not self._closed:
            row = self.store.execute(f"SELECT {column},stop_requested FROM desktop_tasks WHERE id=?",
                                     (task_id,)).fetchone()
            if row is None or row["stop_requested"]:
                raise DesktopCancelled("Emergency stop or cancellation requested")
            if row[column] is not None:
                return row[column]
            self._wake.wait(0.1)
            self._wake.clear()
        raise DesktopCancelled("Desktop controller is shutting down")

    def _update_payload(self, task_id: str, update: dict[str, Any]) -> dict[str, Any]:
        row = self.store.execute("SELECT payload FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        payload = json.loads(row["payload"])
        payload.update(update)
        self._set(task_id, payload=payload)
        return payload

    def _screenshot(self, task_id: str, automation: Any, stage: str, enabled: bool) -> None:
        if not enabled:
            return
        folder = self.root / ".opencode-human-bot" / "screenshots" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{len(self.status(task_id)['screenshots']) + 1:03d}-{stage}.png"
        automation.screenshot().save(path)
        payload = self._update_payload(task_id, {})
        payload["screenshots"].append(path.relative_to(self.root).as_posix())
        self._set(task_id, payload=payload)

    def _execute(self, task_id: str) -> None:
        row = self.store.execute("SELECT * FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        payload = json.loads(row["payload"])
        settings = payload["settings"]
        automation = self._pyautogui()
        self._check(task_id, automation)
        self._set(task_id, status="running", action="Starting VS Code navigation")
        for index, change in enumerate(payload["changes"], 1):
            self._check(task_id, automation)
            target = (self.root / change["path"]).resolve()
            if self.root not in target.parents or not target.is_file():
                raise DesktopError(f"Target file is missing or outside the workspace: {change['path']}")
            if target.read_text(encoding="utf-8") != change["before"]:
                raise DesktopError(f"Target changed after approval: {change['path']}")
            self._set(task_id, step=index, total=len(payload["changes"]), current_path=change["path"],
                      action=f"Opening {change['path']} in Explorer")
            self._open_file(task_id, automation, settings, change["path"])
            time.sleep(settings["action_delay"])
            self._set(task_id, status="verify_file", action="Verifying the active VS Code document",
                      current_path=change["path"], file_verified=None)
            if not self._wait_for(task_id, "file_verified"):
                raise DesktopError("VS Code rejected the active-file verification")
            self._check(task_id, automation)
            self._screenshot(task_id, automation, f"{index}-opened", settings["capture_screenshots"])
            automation.moveTo(settings["editor_x"], settings["editor_y"], duration=settings["mouse_duration"])
            self._check(task_id, automation)
            automation.click(settings["editor_x"], settings["editor_y"])
            self._check(task_id, automation)
            self._set(task_id, status="verify_editor_focus", action="Verifying editor focus before typing",
                      current_path=change["path"], file_verified=None)
            if not self._wait_for(task_id, "file_verified"):
                raise DesktopError("VS Code editor focus was not confirmed")
            self._check(task_id, automation)
            cursor_line = settings.get("cursor_line")
            if cursor_line is not None:
                automation.hotkey("ctrl", "g")
                self._type_chars(task_id, automation, str(cursor_line), settings["typing_interval"])
                self._check(task_id, automation)
                automation.press("enter")
                automation.press("home")
            automation.hotkey("ctrl", "a")
            self._set(task_id, action=f"Typing approved changes into {change['path']}")
            self._type_source(task_id, automation, change["after"], settings["typing_interval"])
            self._check(task_id, automation)
            automation.hotkey("ctrl", "s")
            time.sleep(settings["action_delay"])
            if target.read_text(encoding="utf-8") != change["after"]:
                raise DesktopError(f"Saved file does not match approved content: {change['path']}")
            automation.scroll(-3)
            self._screenshot(task_id, automation, f"{index}-saved", settings["capture_screenshots"])
            payload = self._update_payload(task_id, {"completed_files": payload["completed_files"] + [change["path"]]})
        self.engine.complete_apply(row["plan_id"])
        command = payload.get("command")
        if command:
            self._check(task_id, automation)
            self._set(task_id, action="Opening the integrated terminal for approval")
            automation.hotkey("ctrl", "`")
            time.sleep(settings["action_delay"])
            automation.moveTo(settings["terminal_x"], settings["terminal_y"], duration=settings["mouse_duration"])
            self._check(task_id, automation)
            automation.click(settings["terminal_x"], settings["terminal_y"])
            self._screenshot(task_id, automation, "terminal-open", settings["capture_screenshots"])
            self._set(task_id, status="awaiting_command_approval", action="Waiting for test-command approval")
            if not self._wait_for(task_id, "command_approved"):
                self._set(task_id, status="approval_denied", action="Test command was declined")
                self._return_to_editor(task_id, automation, settings, payload["changes"][-1]["path"])
                self._set(task_id, status="approval_denied", action="Test command declined; edits remain saved")
                return
            self._set(task_id, status="command_ready", action="Waiting for VS Code terminal execution")
            result = json.loads(self._wait_for(task_id, "command_result"))
            if result.get("verified") is not True:
                self._set(task_id, status="verification_required", action="Terminal did not expose a verifiable exit code",
                          command_result=result)
            elif result.get("exit_code") == 0:
                final_status = "completed"
                final_action = "Approved tests passed in the integrated terminal"
            else:
                final_status = "tests_failed"
                final_action = "Approved tests failed in the integrated terminal"
            if result.get("verified") is True:
                self._return_to_editor(task_id, automation, settings, payload["changes"][-1]["path"])
                self._set(task_id, status=final_status, action=final_action, command_result=result)
            return
        self._set(task_id, status="completed", action="All approved files were saved and verified")

    def _return_to_editor(self, task_id: str, automation: Any, settings: dict[str, Any], relative_path: str) -> None:
        self._check(task_id, automation)
        automation.hotkey("ctrl", "`")
        self._open_file(task_id, automation, settings, relative_path)
        time.sleep(settings["action_delay"])
        self._set(task_id, status="verify_file", action="Returning to the completed editor file",
                  current_path=relative_path, file_verified=None)
        if not self._wait_for(task_id, "file_verified"):
            raise DesktopError("VS Code could not confirm the editor after terminal work")
        self._screenshot(task_id, automation, "editor-restored", settings["capture_screenshots"])

    def _open_file(self, task_id: str, automation: Any, settings: dict[str, Any], relative_path: str) -> None:
        automation.moveTo(settings["explorer_x"], settings["explorer_y"], duration=settings["mouse_duration"])
        self._check(task_id, automation)
        automation.click(settings["explorer_x"], settings["explorer_y"])
        self._check(task_id, automation)
        points = settings["explorer_items"].get(relative_path)
        if points:
            for x, y in points[1:] if points[0] == [settings["explorer_x"], settings["explorer_y"]] else points:
                self._check(task_id, automation)
                automation.moveTo(x, y, duration=settings["mouse_duration"])
                automation.click(x, y)
                self._check(task_id, automation)
                time.sleep(0.15)
            return
        automation.hotkey("ctrl", "p")
        self._type_chars(task_id, automation, relative_path.replace("\\", "/"), settings["typing_interval"])
        self._check(task_id, automation)
        automation.press("enter")

    def _type_chars(self, task_id: str, automation: Any, text: str, interval: float) -> None:
        for character in text:
            self._check(task_id, automation)
            automation.write(character, interval=0)
            if interval:
                time.sleep(interval)

    def _type_source(self, task_id: str, automation: Any, text: str, interval: float) -> None:
        for character in text:
            self._check(task_id, automation)
            if character == "\r":
                continue
            if character == "\n":
                automation.press("enter")
            elif character == "\t":
                automation.press("tab")
            else:
                automation.write(character, interval=0)
            if interval:
                time.sleep(interval)

    def _recover_task(self, task_id: str, status: str, error: str) -> None:
        row = self.store.execute("SELECT plan_id FROM desktop_tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return
        plan = self.engine.get_plan(row["plan_id"])
        if plan["status"] == "applying":
            try:
                self.engine.abort_apply(row["plan_id"])
            except Exception as rollback_error:  # noqa: BLE001
                error = f"{error}; checkpoint restoration failed: {rollback_error}"
        self._set(task_id, status=status, error=error[:2000], action="Stopped; review changes and checkpoint before continuing")
        self._log(f"Task {task_id} {status}: {error[:300]}")
