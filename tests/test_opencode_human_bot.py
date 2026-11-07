from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

import pytest

from opencode_human_bot.desktop import DesktopController, DesktopError
from opencode_human_bot.engine import CodingEngine, safe_path
from opencode_human_bot.languages import ADAPTERS, LanguageAdapter


def fake_parse(self: LanguageAdapter, source: bytes):
    text = source.decode().replace("\r\n", "\n")
    match = re.search(r"def (\w+)\([^\n]*\):\n(?:    .*\n)+", text)
    if not match:
        return [], None
    return [{"kind": "function", "name": match.group(1), "line": 1,
             "end_line": text.count("\n") + 1, "source": match.group(0)}], None


@pytest.fixture
def repositories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(LanguageAdapter, "parse", fake_parse)
    target = tmp_path / "target"
    source = tmp_path / "source"
    target.mkdir()
    source.mkdir()
    (target / "pyproject.toml").write_text('[project]\nname="target"\n')
    (target / "target.py").write_text("def calculate(value):\n    return value\n")
    (source / "LICENSE").write_text("MIT License\npermission granted")
    (source / "math_utils.py").write_text("def calculate(value):\n    return value + 1\n")
    engine = CodingEngine(target, tmp_path / "state.sqlite3")
    engine.add_local_repository(source)
    engine.index_repository(source)
    yield engine, target, source
    engine.close()


def test_index_search_preserves_source_attribution(repositories):
    engine, _, _ = repositories
    matches = engine.search("calculate", language="Python")
    assert len(matches) == 1
    assert matches[0]["name"] == "calculate"
    assert matches[0]["licensed"] is True
    assert matches[0]["license"].startswith("MIT License")
    assert matches[0]["compatibility"]["language_match"] is True
    assert matches[0]["compatibility"]["dependency_versions_verified"] is False


def test_plan_requires_approval_and_rollback_restores_target(repositories):
    engine, target, _ = repositories
    plan = engine.create_plan("calculate Python")
    assert plan["status"] == "awaiting_approval"
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py",
                                        "return value", "return value + 1")
    assert "MIT License" in plan["changes"][0]["attribution"]["license"]
    with pytest.raises(PermissionError):
        engine.apply_plan(plan["id"])

    result = engine.apply_plan(plan["id"], approved=True)
    assert (target / "target.py").read_text().endswith("return value + 1\n")
    assert result["checkpoint"]["snapshotted_files"] == ["target.py"]

    with pytest.raises(PermissionError):
        engine.rollback(result["checkpoint"]["id"])
    engine.rollback(result["checkpoint"]["id"], approved=True)
    assert (target / "target.py").read_text().endswith("return value\n")


def test_path_escape_and_terminal_approval_are_rejected(repositories):
    engine, target, _ = repositories
    with pytest.raises(ValueError, match="escapes"):
        safe_path(target, "../outside.py")
    with pytest.raises(PermissionError, match="approval"):
        engine.run_command([sys.executable, "-c", "print('unsafe')"])


def test_text_search_is_attributed_and_commands_do_not_inherit_secrets(repositories, monkeypatch):
    engine, _, _ = repositories
    matches = engine.search_text("return value + 1")
    assert matches
    assert matches[0]["repository"] == "source"
    assert matches[0]["path"] == "math_utils.py"
    assert matches[0]["license"].startswith("MIT License")

    monkeypatch.setenv("TEST_API_TOKEN", "private-token-value")
    result = engine.run_command(
        [sys.executable, "-c", "import os; print(os.environ.get('TEST_API_TOKEN', 'missing'))"],
        approved=True,
    )
    assert result["returncode"] == 0
    assert "missing" in result["stdout"]
    assert "private-token-value" not in result["stdout"]


def test_desktop_start_requires_approval_and_coordinates_are_bounded(repositories):
    engine, _, _ = repositories
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")
    with pytest.raises(PermissionError, match="approval"):
        engine.desktop.start(plan["id"], {})
    with pytest.raises(ValueError, match="outside the screen"):
        DesktopController._validate_settings({"editor_x": 800}, (800, 600))
    assert DesktopController._is_vscode_process("Code.exe")
    assert not DesktopController._is_vscode_process("notepad.exe")


def test_desktop_start_refuses_unrelated_foreground_application(repositories, monkeypatch):
    engine, _, _ = repositories
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")

    class FakeDesktop:
        @staticmethod
        def size():
            return 800, 600

    monkeypatch.setattr(DesktopController, "_pyautogui", staticmethod(lambda: FakeDesktop()))
    monkeypatch.setattr(DesktopController, "_foreground_process", staticmethod(lambda: "notepad.exe"))
    with pytest.raises(RuntimeError, match="foreground application"):
        engine.desktop.start(plan["id"], {}, approved=True)
    assert engine.get_plan(plan["id"])["status"] == "awaiting_approval"


def test_desktop_start_refuses_another_vscode_workspace(repositories, monkeypatch):
    engine, _, _ = repositories
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")
    monkeypatch.setattr(DesktopController, "_pyautogui", staticmethod(lambda: type("Desktop", (), {"size": lambda self: (800, 600)})()))
    monkeypatch.setattr(DesktopController, "_foreground_process", staticmethod(lambda: "code.exe"))
    monkeypatch.setattr(DesktopController, "_foreground_title", staticmethod(lambda: "other-project - Visual Studio Code"))
    with pytest.raises(RuntimeError, match="does not match"):
        engine.desktop.start(plan["id"], {}, approved=True)
    assert engine.get_plan(plan["id"])["status"] == "awaiting_approval"


def test_desktop_worker_pins_window_handle_not_only_process(repositories, monkeypatch):
    engine, _, _ = repositories
    task_id = "desktop-window-pin-test"
    engine.store.execute(
        "INSERT INTO desktop_tasks(id,plan_id,status,payload,checkpoint,created_at,updated_at) "
        "VALUES(?,?,'running',?,?,?,?)",
        (task_id, "plan", engine.store.encode({"window_identity": {"hwnd": 101, "pid": 900}}),
         engine.store.encode({}), "now", "now"),
    )
    engine.store.commit()
    monkeypatch.setattr(DesktopController, "_foreground_process", staticmethod(lambda: "code.exe"))
    monkeypatch.setattr(DesktopController, "_foreground_title", staticmethod(lambda: "target - Visual Studio Code"))
    monkeypatch.setattr(DesktopController, "_foreground_identity", staticmethod(lambda: {"hwnd": 202, "pid": 900}))

    with pytest.raises(DesktopError, match="exact VS Code window"):
        engine.desktop._check(task_id, object())


def test_desktop_terminal_approval_and_result_are_persisted(repositories):
    engine, _, _ = repositories
    task_id = "desktop-command-approval-test"
    engine.store.execute(
        "INSERT INTO desktop_tasks(id,plan_id,status,payload,checkpoint,created_at,updated_at) "
        "VALUES(?,?,'awaiting_command_approval',?,?,?,?)",
        (task_id, "plan", engine.store.encode({"command": ["pytest"]}),
         engine.store.encode({}), "now", "now"),
    )
    engine.store.commit()

    approved = engine.desktop.approve_command(task_id, True)
    assert approved["status"] == "command_ready"
    result = engine.desktop.report_command(
        task_id, {"verified": True, "exit_code": 0, "output": "1 passed"})
    assert result["status"] == "command_result"
    assert result["command_result"]["exit_code"] == 0


def test_repair_classifies_known_diagnostics_and_rejects_unknown(repositories):
    engine, _, _ = repositories
    known = engine.repairs.classify("target.py:1:0: W292 no newline at end of file")
    assert known["recognized"] is True
    assert known["rule_id"] == "PY_W292"
    assert engine.repairs.classify("unclassified error message")["recognized"] is False


def test_repair_requires_approval_validates_and_rolls_back_on_failure(repositories):
    engine, target, _ = repositories
    target_file = target / "target.py"
    target_file.write_text("def calculate(value):\n    return value", encoding="utf-8")
    diagnostic = "target.py:2:17: W292 no newline at end of file"
    plan = engine.repairs.propose(diagnostic, "target.py")
    assert plan["status"] == "repair_pending"
    assert plan["candidates"][0]["recipe_id"] == "PY_W292_APPEND_NEWLINE"
    with pytest.raises(PermissionError, match="approval"):
        engine.repairs.apply(plan["id"], 0, [sys.executable, "-m", "py_compile", "target.py"])

    success = engine.repairs.apply(
        plan["id"], 0, [sys.executable, "-m", "py_compile", "target.py"], approved=True)
    assert success["verified"] is True
    assert target_file.read_text(encoding="utf-8").endswith("\n")

    target_file.write_text("def calculate(value):\n    return value", encoding="utf-8")
    verified_recipe = engine.repairs.propose(diagnostic, "target.py")
    assert verified_recipe["candidates"][0]["source"] == "verified_database"
    retry = engine.repairs.propose(diagnostic, "target.py")
    failure = engine.repairs.apply(
        retry["id"], 0, [sys.executable, "-c", "raise SystemExit(2)"], approved=True)
    assert failure["status"] == "rolled_back"
    assert target_file.read_text(encoding="utf-8") == "def calculate(value):\n    return value"


def test_repair_searches_licensed_repository_recipes(repositories):
    engine, target, source = repositories
    (source / ".opencode-repairs.json").write_text(
        '{"repairs":[{"id":"fixture-w292","rule_id":"PY_W292",'
        '"language":"Python","target_glob":"*.py","operation":"append_newline"}]}',
        encoding="utf-8",
    )
    engine.add_local_repository(source)
    (target / "target.py").write_text("def calculate(value):\n    return value", encoding="utf-8")

    plan = engine.repairs.propose("target.py:2:17: W292 no newline at end of file", "target.py")

    assert plan["candidates"][0]["recipe_id"] == "fixture-w292"
    assert plan["candidates"][0]["repository"] == "source"
    assert plan["candidates"][0]["license"].startswith("MIT License")


def test_browser_runner_rejects_external_urls(repositories):
    engine, _, _ = repositories
    with pytest.raises(ValueError, match="localhost"):
        engine.browser.run("https://example.com", [{"action": "navigate", "url": "/"}], approved=True)


def test_playwright_local_form_flow_and_screenshots(tmp_path: Path):
    pytest.importorskip("playwright.sync_api")
    project = tmp_path / "browser-app"
    project.mkdir()
    (project / "index.html").write_text("""<!doctype html>
<html><body>
<form id="greet-form">
  <label>Name <input id="name" name="name" required></label>
  <label>Role <select id="role"><option value="builder">Builder</option><option value="reviewer">Reviewer</option></select></label>
  <label><input id="terms" type="checkbox"> Accept terms</label>
  <button id="submit" type="submit">Submit</button>
</form>
<output id="result"></output>
<script>
document.querySelector('#greet-form').addEventListener('submit', event => {
  event.preventDefault();
    document.querySelector('#result').textContent = `Hello ${document.querySelector('#name').value} | ${document.querySelector('#role').value}`;
});
</script>
</body></html>""", encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    engine = CodingEngine(project, tmp_path / "browser-state.sqlite3")
    try:
        report = engine.browser.run(
            f"http://127.0.0.1:{port}/",
            [
                {"action": "navigate", "url": "/"},
                {"action": "fill", "selector": "#name", "value": "Ada"},
                {"action": "select", "selector": "#role", "value": "reviewer"},
                {"action": "check", "selector": "#terms"},
                {"action": "screenshot", "name": "form-filled"},
                {"action": "click", "selector": "#submit"},
                {"action": "expect_text", "selector": "#result", "value": "Hello Ada | reviewer"},
                {"action": "screenshot", "name": "form-result"},
            ],
            approved=True,
            application_command=[sys.executable, "-m", "http.server", str(port),
                                 "--bind", "127.0.0.1", "--directory", str(project)],
        )
    finally:
        engine.close()

    assert report["status"] == "passed", [step for step in report["steps"] if step["status"] == "failed"]
    assert report["verified"] is True
    assert len(report["steps"]) == 8
    assert len(report["screenshots"]) == 2
    assert all((project / item).is_file() and (project / item).stat().st_size > 0
               for item in report["screenshots"])


def test_desktop_failure_restores_checkpoint_before_retry(repositories):
    engine, target, _ = repositories
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")
    engine.prepare_apply(plan["id"], approved=True)
    (target / "target.py").write_text("def calculate(value):\n    return value + 1\n")
    task_id = "desktop-interruption-test"
    engine.store.execute(
        "INSERT INTO desktop_tasks(id,plan_id,status,payload,checkpoint,created_at,updated_at) "
        "VALUES(?,?,'running',?,?,?,?)",
        (task_id, plan["id"], engine.store.encode({"completed_files": ["target.py"]}),
         engine.store.encode({}), "now", "now"),
    )
    engine.store.commit()

    engine.desktop._recover_task(task_id, "blocked", "focus left VS Code")

    assert (target / "target.py").read_text().endswith("return value\n")
    assert engine.get_plan(plan["id"])["status"] == "awaiting_approval"


def test_engine_restart_restores_stale_physical_task(repositories):
    engine, target, _ = repositories
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")
    prepared = engine.prepare_apply(plan["id"], approved=True)
    (target / "target.py").write_text("def calculate(value):\n    return value + 1\n")
    task_id = "desktop-stale-task"
    engine.store.execute(
        "INSERT INTO desktop_tasks(id,plan_id,status,payload,checkpoint,created_at,updated_at) "
        "VALUES(?,?,'running',?,?,?,?)",
        (task_id, plan["id"], engine.store.encode({"completed_files": ["target.py"]}),
         engine.store.encode(prepared["checkpoint"]), "now", "now"),
    )
    engine.store.commit()

    restarted = CodingEngine(target, engine.store.path)
    try:
        task = restarted.desktop.status(task_id)
        assert task["status"] == "interrupted"
        assert (target / "target.py").read_text().endswith("return value\n")
        assert restarted.get_plan(plan["id"])["status"] == "awaiting_approval"
    finally:
        restarted.close()


def test_desktop_worker_opens_verifies_types_and_saves(repositories, monkeypatch):
    engine, target, _ = repositories

    class FakeDesktop:
        def __init__(self):
            self.buffer = ""
            self.opened = None
            self.navigating = False
            self.actions = []

        def size(self):
            return 800, 600

        def moveTo(self, x, y, duration=0):
            self.actions.append(("move", x, y))

        def click(self, x=None, y=None):
            self.actions.append(("click", x, y))

        def hotkey(self, *keys):
            self.actions.append(("hotkey", *keys))
            if keys == ("ctrl", "p"):
                self.navigating = True
            elif keys == ("ctrl", "a"):
                self.buffer = ""
                self.navigating = False
            elif keys == ("ctrl", "s") and self.opened:
                self.opened.write_text(self.buffer)

        def write(self, text, interval=0):
            self.buffer += text

        def press(self, key):
            if key == "enter":
                if self.navigating:
                    self.opened = target / self.buffer.replace("/", "\\")
                    self.buffer = ""
                    self.navigating = False
                else:
                    self.buffer += "\n"

        def scroll(self, amount):
            self.actions.append(("scroll", amount))

    fake = FakeDesktop()
    monkeypatch.setattr(DesktopController, "_pyautogui", staticmethod(lambda: fake))
    monkeypatch.setattr(DesktopController, "_foreground_process", staticmethod(lambda: "code.exe"))
    monkeypatch.setattr(DesktopController, "_foreground_title", staticmethod(lambda: "target - Visual Studio Code"))
    plan = engine.create_plan("calculate Python")
    plan = engine.add_exact_replacement(plan["id"], 0, "target.py", "return value", "return value + 1")

    task = engine.desktop.start(plan["id"], {"capture_screenshots": False,
                                              "typing_interval": 0, "action_delay": 0}, approved=True)
    deadline = __import__("time").monotonic() + 8
    while __import__("time").monotonic() < deadline:
        current = engine.desktop.status(task["id"])
        if current["status"] in {"verify_file", "verify_editor_focus"}:
            engine.desktop.acknowledge_file(task["id"], True)
        elif current["status"] in {"completed", "blocked", "cancelled"}:
            break
        __import__("time").sleep(0.01)

    final = engine.desktop.status(task["id"])
    assert final["status"] == "completed"
    assert final["completed_files"] == ["target.py"]
    assert (target / "target.py").read_text().endswith("return value + 1\n")
    assert any(action[0] == "move" for action in fake.actions)
    assert any(action[0] == "scroll" for action in fake.actions)


def test_unlicensed_source_is_not_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(LanguageAdapter, "parse", fake_parse)
    target = tmp_path / "target"
    source = tmp_path / "source"
    target.mkdir()
    source.mkdir()
    (target / "target.py").write_text("def calculate(value):\n    return value\n")
    (source / "sample.py").write_text("def calculate(value):\n    return value + 1\n")
    engine = CodingEngine(target, tmp_path / "state.sqlite3")
    try:
        engine.add_local_repository(source)
        engine.index_repository(source)
        plan = engine.create_plan("calculate Python")
        assert plan["status"] == "needs_source"
        assert plan["candidates"] == []
    finally:
        engine.close()


@pytest.mark.parametrize("language,source", [
    ("Python", "def greet():\n    return 'hi'\nclass Greeter:\n    pass\n"),
    ("JavaScript", "function greet() { return 'hi'; } class Greeter {}"),
    ("TypeScript", "function greet(): string { return 'hi'; } class Greeter {}"),
    ("PHP", "<?php function greet() { return 'hi'; } class Greeter {}"),
    ("Rust", "fn greet() -> &'static str { \"hi\" } struct Greeter;"),
    ("Go", "package main\nimport \"fmt\"\nfunc greet() string { return fmt.Sprint(1) }\ntype Greeter struct {}"),
    ("Java", "class Greeter { String greet() { return \"hi\"; } }"),
    ("C", "struct Greeter {}; int greet(void) { return 1; }"),
    ("C++", "class Greeter {}; int greet() { return 1; }"),
    ("C#", "class Greeter { string Greet() { return \"hi\"; } }"),
    ("Ruby", "def greet\n  'hi'\nend\nclass Greeter\nend\n"),
    ("Kotlin", "fun greet() = \"hi\"\nclass Greeter"),
    ("Swift", "func greet() -> String { \"hi\" }\nclass Greeter {}"),
])
def test_language_adapter_parses_real_grammar(language: str, source: str):
    adapter = next(adapter for adapter in ADAPTERS if adapter.name == language)
    if not adapter.parser_available:
        pytest.skip("Tree-sitter language pack is not installed")
    symbols, error = adapter.parse(source.encode())
    assert error is None
    assert symbols, f"No function or class structures extracted for {language}"