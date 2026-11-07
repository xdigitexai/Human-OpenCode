from __future__ import annotations

import importlib
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SAFE_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME"}
ALLOWED_STEPS = {"navigate", "click", "fill", "check", "uncheck", "select", "expect_text", "expect_visible", "screenshot"}


class BrowserAutomation:
    def __init__(self, engine: Any):
        self.engine = engine

    @staticmethod
    def _local_url(value: str, base_url: str | None = None) -> str:
        url = urllib.parse.urljoin(base_url or "", value)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOCAL_HOSTS:
            raise ValueError("Browser automation is restricted to localhost and loopback URLs")
        return url

    @classmethod
    def _validate_steps(cls, base_url: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cls._local_url(base_url)
        if not isinstance(steps, list) or not steps or len(steps) > 100:
            raise ValueError("A browser scenario must contain between 1 and 100 steps")
        validated = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict) or step.get("action") not in ALLOWED_STEPS:
                raise ValueError(f"Browser step {index + 1} has an unsupported action")
            action = step["action"]
            if action == "navigate":
                if not isinstance(step.get("url"), str):
                    raise ValueError(f"Browser step {index + 1} requires a URL")
                step = {**step, "url": cls._local_url(step["url"], base_url)}
            elif action == "screenshot":
                if not isinstance(step.get("name", f"step-{index + 1}"), str):
                    raise ValueError(f"Browser step {index + 1} has an invalid screenshot name")
            else:
                if not isinstance(step.get("selector"), str) or not step["selector"].strip():
                    raise ValueError(f"Browser step {index + 1} requires a selector")
                if action in {"fill", "select", "expect_text"} and not isinstance(step.get("value"), str):
                    raise ValueError(f"Browser step {index + 1} requires a string value")
            validated.append(step)
        return validated

    def run(self, base_url: str, steps: list[dict[str, Any]], approved: bool = False,
            application_command: list[str] | None = None, ready_timeout: float = 30,
            step_timeout_ms: int = 10000, headless: bool = True) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Browser navigation and interaction require explicit user approval")
        validated = self._validate_steps(base_url, steps)
        if application_command is not None and (not application_command or
                any(not isinstance(part, str) or "\x00" in part for part in application_command)):
            raise ValueError("Application launch command must be a non-empty argument array")
        try:
            playwright = importlib.import_module("playwright.sync_api")
        except ImportError as error:
            return {"status": "blocked", "verified": False,
                    "error": "Install the optional browser extra and a Playwright Chromium browser.",
                    "detail": str(error), "steps": [], "screenshots": []}

        server = None
        results: list[dict[str, Any]] = []
        screenshots: list[str] = []
        started = time.monotonic()
        if application_command is not None:
            safe_env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV}
            server = subprocess.Popen(application_command, cwd=self.engine.root, env=safe_env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not self._wait_ready(base_url, server, ready_timeout):
                self._stop_process(server)
                result = {"status": "failed", "verified": True, "error": "Local application did not become ready.",
                          "steps": [], "screenshots": [], "duration_seconds": round(time.monotonic() - started, 3)}
                self.engine._log("browser", f"Local application failed readiness: {application_command[0]}")
                return result

        screenshot_root = self.engine.root / ".opencode-human-bot" / "browser-screenshots"
        screenshot_root.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex[:12]
        page = None
        try:
            with playwright.sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=headless)
                context = browser.new_context()

                def restrict_external(route: Any) -> None:
                    try:
                        self._local_url(route.request.url)
                    except ValueError:
                        route.abort()
                    else:
                        route.continue_()

                context.route("**/*", restrict_external)
                page = context.new_page()
                page.set_default_timeout(min(max(step_timeout_ms, 250), 60000))
                failed = None
                for index, step in enumerate(validated, 1):
                    action = step["action"]
                    item = {"step": index, "action": action, "selector": step.get("selector"), "status": "passed"}
                    before = time.monotonic()
                    try:
                        if action == "navigate":
                            page.goto(step["url"], wait_until="domcontentloaded")
                        elif action == "click":
                            page.locator(step["selector"]).click()
                        elif action == "fill":
                            page.locator(step["selector"]).fill(step["value"])
                        elif action == "check":
                            page.locator(step["selector"]).check()
                        elif action == "uncheck":
                            page.locator(step["selector"]).uncheck()
                        elif action == "select":
                            page.locator(step["selector"]).select_option(step["value"])
                        elif action == "expect_text":
                            playwright.expect(page.locator(step["selector"])).to_contain_text(step["value"])
                        elif action == "expect_visible":
                            page.locator(step["selector"]).wait_for(state="visible")
                        elif action == "screenshot":
                            name = re.sub(r"[^A-Za-z0-9_.-]+", "-", step.get("name", f"step-{index}"))[:80]
                            path = screenshot_root / f"{run_id}-{index:02d}-{name}.png"
                            page.screenshot(path=str(path), full_page=True)
                            screenshots.append(path.relative_to(self.engine.root).as_posix())
                            item["screenshot"] = screenshots[-1]
                    except Exception as error:  # noqa: BLE001
                        item["status"] = "failed"
                        item["error"] = str(error)[:2000]
                        failed = item
                    item["duration_ms"] = round((time.monotonic() - before) * 1000, 1)
                    results.append(item)
                    if failed:
                        break
                browser.close()
            passed = failed is None
            report = {"status": "passed" if passed else "failed", "verified": True,
                      "url": base_url, "steps": results, "screenshots": screenshots,
                      "duration_seconds": round(time.monotonic() - started, 3)}
            self.engine._log("browser", f"Browser scenario {report['status']}: {len(results)} step(s)")
            return report
        except Exception as error:  # noqa: BLE001
            report = {"status": "failed", "verified": True, "url": base_url,
                      "steps": results, "screenshots": screenshots, "error": str(error)[:2000],
                      "duration_seconds": round(time.monotonic() - started, 3)}
            self.engine._log("browser", f"Browser scenario failed before completion: {type(error).__name__}")
            return report
        finally:
            if server is not None:
                self._stop_process(server)

    @staticmethod
    def _wait_ready(url: str, process: subprocess.Popen[Any], timeout: float) -> bool:
        deadline = time.monotonic() + min(max(timeout, 0), 120)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(url, timeout=1):
                    return True
            except (OSError, urllib.error.URLError):
                time.sleep(0.2)
        return False

    @staticmethod
    def _stop_process(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)