from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .engine import CodingEngine


def dispatch(engine: CodingEngine, request: dict[str, Any]) -> Any:
    operation = request.get("op")
    if operation == "inspect":
        return engine.inspect()
    if operation == "index":
        return engine.index_repository(Path(request["path"]) if request.get("path") else None)
    if operation == "add_local_repository":
        return engine.add_local_repository(Path(request["path"]))
    if operation == "repositories":
        return [dict(row) for row in engine.store.execute(
            "SELECT id,root,name,url,license,indexed_at FROM repositories ORDER BY name").fetchall()]
    if operation == "search_github":
        return engine.search_github(request["query"], request.get("limit", 10))
    if operation == "clone":
        return engine.clone_repository(request["url"], request.get("approved") is True)
    if operation == "search":
        return engine.search(request["query"], request.get("language"), request.get("limit", 30))
    if operation == "search_text":
        return engine.search_text(request["query"], request.get("limit", 50))
    if operation == "plan":
        return engine.create_plan(request["task"], request.get("limit", 5))
    if operation == "add_replacement":
        return engine.add_exact_replacement(request["plan_id"], request["candidate_index"],
                                            request["path"], request["old"], request["new"])
    if operation == "get_plan":
        return engine.get_plan(request["plan_id"])
    if operation == "prepare_apply":
        return engine.prepare_apply(request["plan_id"], request.get("approved") is True)
    if operation == "complete_apply":
        return engine.complete_apply(request["plan_id"])
    if operation == "abort_apply":
        return engine.abort_apply(request["plan_id"])
    if operation == "apply":
        return engine.apply_plan(request["plan_id"], request.get("approved") is True,
                                 request.get("mode", "native"))
    if operation == "command":
        return engine.run_command(request["command"], request.get("approved") is True,
                                  request.get("timeout", 300))
    if operation == "rollback":
        return engine.rollback(request["checkpoint_id"], request.get("approved") is True)
    if operation == "screenshot":
        return engine.capture_screenshot(request.get("approved") is True)
    if operation == "history":
        return engine.history(request.get("limit", 100))
    if operation == "desktop_start":
        return engine.desktop.start(request["plan_id"], request.get("settings", {}),
                                     request.get("command"), request.get("approved") is True)
    if operation == "desktop_status":
        return engine.desktop.status(request.get("task_id"), request.get("limit", 20))
    if operation == "desktop_verify_file":
        return engine.desktop.acknowledge_file(request["task_id"], request.get("verified") is True)
    if operation == "desktop_verify_focus":
        return engine.desktop.acknowledge_file(request["task_id"], request.get("verified") is True)
    if operation == "desktop_approve_command":
        return engine.desktop.approve_command(request["task_id"], request.get("approved") is True)
    if operation == "desktop_report_command":
        return engine.desktop.report_command(request["task_id"], request["result"])
    if operation == "desktop_cancel":
        return engine.desktop.cancel(request.get("task_id"))
    if operation == "propose_repair":
        return engine.repairs.propose(request["diagnostic"], request["path"])
    if operation == "apply_repair":
        return engine.repairs.apply(request["plan_id"], request["candidate_index"],
                                    request["validation_command"], request.get("approved") is True)
    if operation == "browser_run":
        return engine.browser.run(request["base_url"], request["steps"],
                                  request.get("approved") is True,
                                  request.get("application_command"),
                                  request.get("ready_timeout", 30), request.get("step_timeout_ms", 10000),
                                  request.get("headless", True))
    if operation == "agent_start":
        return engine.autonomous.start(
            Path(request["repository"]) if request.get("repository") else engine.root,
            request["task"], request.get("provider", "openai"), request.get("model"),
            request.get("base_url"), request.get("api_key_env"),
            request.get("duration_seconds", 14_400), request.get("max_iterations", 50),
            request.get("approved") is True, request.get("api_key"), request.get("pace_seconds", 0))
    if operation == "agent_status":
        if request.get("session_id"):
            return engine.autonomous.status(request["session_id"])
        return engine.autonomous.list_sessions(request.get("limit", 20))
    if operation == "agent_pause":
        return engine.autonomous.pause(request["session_id"])
    if operation == "agent_resume" or operation == "agent_restart":
        return engine.autonomous.resume(request["session_id"], request.get("api_key"))
    if operation == "agent_stop":
        return engine.autonomous.stop(request["session_id"])
    if operation == "agent_report":
        return engine.autonomous.report(request["session_id"])
    if operation == "agent_validate":
        return engine.autonomous.run_validation(request["session_id"], request["command"],
                                                request.get("approved") is True,
                                                request.get("timeout", 300))
    if operation == "agent_diff":
        return {"session_id": request["session_id"], "diff": engine.autonomous.diff(request["session_id"])}
    if operation == "agent_rollback":
        return engine.autonomous.rollback(request["session_id"], request["checkpoint_id"],
                                          request.get("approved") is True)
    raise ValueError(f"Unknown operation: {operation}")


def main() -> int:
    parser = argparse.ArgumentParser(prog="opencode-human-bot")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    try:
        engine = CodingEngine(args.root, args.database)
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ready": False, "error": str(error)}), flush=True)
        return 2
    print(json.dumps({"ready": True}), flush=True)
    try:
        for line in sys.stdin:
            try:
                result = dispatch(engine, json.loads(line))
                response = {"ok": True, "result": result}
            except Exception as error:  # noqa: BLE001
                response = {"ok": False, "error": str(error), "type": type(error).__name__}
            print(json.dumps(response, ensure_ascii=True), flush=True)
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
