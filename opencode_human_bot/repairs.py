from __future__ import annotations

import difflib
import fnmatch
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .engine import safe_path, utc_now


class RepairEngine:
    def __init__(self, engine: Any):
        self.engine = engine
        self.store = engine.store

    @staticmethod
    def classify(diagnostic: str) -> dict[str, Any]:
        patterns = (
            ("PY_W292", "Python", r"\bW292\b|no newline at end of file"),
            ("PY_UNCLOSED_DELIMITER", "Python", r"SyntaxError:.*(?:was never closed|unexpected EOF)"),
            ("PY_MODULE_NOT_FOUND", "Python", r"ModuleNotFoundError: No module named ['\"]?([^'\"\s]+)"),
        )
        for rule_id, language, pattern in patterns:
            match = re.search(pattern, diagnostic, re.IGNORECASE | re.MULTILINE)
            if match:
                return {"recognized": True, "rule_id": rule_id, "language": language,
                        "detail": match.group(1) if match.lastindex else match.group(0)}
        typescript = re.search(r"\bTS(\d{4})\b", diagnostic)
        if typescript:
            return {"recognized": True, "rule_id": f"TS_{typescript.group(1)}",
                    "language": "TypeScript", "detail": diagnostic.strip()[:500]}
        rust = re.search(r"\berror\[(E\d+)\]", diagnostic)
        if rust:
            return {"recognized": True, "rule_id": f"RUST_{rust.group(1)}",
                    "language": "Rust", "detail": diagnostic.strip()[:500]}
        go = re.search(r"\bundefined:\s+([A-Za-z_]\w*)", diagnostic)
        if go:
            return {"recognized": True, "rule_id": "GO_UNDEFINED",
                    "language": "Go", "detail": go.group(1)}
        return {"recognized": False, "rule_id": None, "language": None,
                "detail": "No predefined diagnostic pattern matched."}

    def _repository_recipes(self, rule_id: str, language: str, relative_path: str) -> list[dict[str, Any]]:
        rows = self.store.execute("SELECT id,root,name,url,license FROM repositories WHERE license IS NOT NULL").fetchall()
        found = []
        for row in rows:
            root = Path(row["root"])
            manifest = root / ".opencode-repairs.json"
            if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 250_000:
                continue
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            for recipe in payload.get("repairs", []) if isinstance(payload, dict) else []:
                if not isinstance(recipe, dict):
                    continue
                if recipe.get("rule_id") != rule_id or recipe.get("language") != language:
                    continue
                if not fnmatch.fnmatch(relative_path, recipe.get("target_glob", "*")):
                    continue
                if recipe.get("operation") not in {"exact_replace", "append_newline"}:
                    continue
                if recipe.get("operation") == "exact_replace" and not all(
                        isinstance(recipe.get(key), str) for key in ("before", "after")):
                    continue
                found.append({"repository": row["name"], "repository_url": row["url"],
                              "license": row["license"], "recipe_id": recipe.get("id", rule_id),
                              "operation": recipe["operation"], "before": recipe.get("before"),
                              "after": recipe.get("after"), "source": "repository_recipe"})
        return found

    def _verified_recipes(self, rule_id: str, language: str, relative_path: str) -> list[dict[str, Any]]:
        rows = self.store.execute(
            "SELECT * FROM verified_repairs WHERE rule_id=? AND language=? ORDER BY verified_at DESC",
            (rule_id, language),
        ).fetchall()
        return [{"repository": row["repository"], "repository_url": row["repository_url"],
                 "license": row["license"], "recipe_id": row["id"],
                 "operation": row["operation"], "before": row["before_text"],
                 "after": row["after_text"], "source": "verified_database",
                 "target_glob": row["target_glob"]}
                for row in rows if fnmatch.fnmatch(relative_path, row["target_glob"])]

    def propose(self, diagnostic: str, relative_path: str) -> dict[str, Any]:
        classification = self.classify(diagnostic)
        if not classification["recognized"]:
            return {"status": "unrecognized", "classification": classification, "candidates": []}
        path = safe_path(self.engine.root, relative_path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Repair target must be an existing regular file inside the workspace")
        original = path.read_text(encoding="utf-8")
        candidates = self._verified_recipes(classification["rule_id"], classification["language"], relative_path)
        candidates.extend(self._repository_recipes(classification["rule_id"], classification["language"], relative_path))
        if classification["rule_id"] == "PY_W292" and not original.endswith(("\n", "\r")):
            candidates.append({"repository": "OpenCode Human Bot predefined rule",
                              "repository_url": None, "license": "Built-in deterministic formatting repair",
                              "recipe_id": "PY_W292_APPEND_NEWLINE", "operation": "append_newline",
                              "before": None, "after": None, "source": "predefined_rule"})
        planned = []
        for candidate in candidates:
            if candidate["operation"] == "append_newline":
                updated = original + ("\r\n" if "\r\n" in original else "\n")
            else:
                before = candidate["before"]
                if not before or original.count(before) != 1:
                    continue
                updated = original.replace(before, candidate["after"], 1)
            if updated == original:
                continue
            planned.append({**candidate, "before_text": original, "after_text": updated,
                            "diff": "".join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                                                                  fromfile=relative_path, tofile=relative_path))})
        if not planned:
            return {"status": "no_compatible_fix", "classification": classification,
                    "candidates": [], "searched_repositories": len(self.store.execute(
                        "SELECT id FROM repositories").fetchall())}
        plan_id = uuid.uuid4().hex
        plan = {"id": plan_id, "kind": "repair", "target": relative_path,
                "classification": classification, "candidates": planned,
                "status": "repair_pending", "notice": "Review the exact one-file change and validation command before approval."}
        self.store.execute("INSERT INTO plans(id,payload,status) VALUES(?,?,?)",
                           (plan_id, self.store.encode(plan), plan["status"]))
        self.store.commit()
        self.engine._log("repair", f"Proposed {classification['rule_id']} repair for {relative_path}")
        return plan

    def apply(self, plan_id: str, candidate_index: int, validation_command: list[str],
              approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Applying a repair and running its validation require explicit approval")
        if not validation_command or any(not isinstance(part, str) or "\x00" in part for part in validation_command):
            raise ValueError("A validation command argument array is required")
        row = self.store.execute("SELECT payload,status FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None or row["status"] != "repair_pending":
            raise ValueError("Repair plan is missing or no longer pending")
        plan = json.loads(row["payload"])
        if candidate_index < 0 or candidate_index >= len(plan["candidates"]):
            raise ValueError("Choose a repair candidate shown in the plan")
        candidate = plan["candidates"][candidate_index]
        target = safe_path(self.engine.root, plan["target"])
        if target.is_symlink() or not target.is_file():
            raise ValueError("Repair target is missing or no longer a regular file")
        if target.read_text(encoding="utf-8") != candidate["before_text"]:
            raise RuntimeError("Repair target changed after the proposal was created")
        checkpoint = self.engine.checkpoint([plan["target"]])
        target.write_text(candidate["after_text"], encoding="utf-8", newline="")
        try:
            validation = self.engine.run_command(validation_command, approved=True)
        except Exception as error:  # noqa: BLE001
            self.engine.rollback(checkpoint["id"], approved=True)
            result = {"status": "rolled_back", "verified": False, "error": str(error),
                      "checkpoint": checkpoint, "validation": None}
        else:
            passed = validation["returncode"] == 0
            if not passed:
                self.engine.rollback(checkpoint["id"], approved=True)
            else:
                self.store.execute(
                    "INSERT OR IGNORE INTO verified_repairs "
                    "(id,rule_id,language,target_glob,operation,before_text,after_text,repository,repository_url,license,verified_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (candidate["recipe_id"], plan["classification"]["rule_id"],
                     plan["classification"]["language"], candidate.get("target_glob", "*"),
                     candidate["operation"], candidate.get("before"), candidate.get("after"),
                     candidate["repository"], candidate["repository_url"], candidate["license"], utc_now()),
                )
                self.store.commit()
            result = {"status": "verified" if passed else "rolled_back", "verified": passed,
                      "checkpoint": checkpoint, "validation": validation,
                      "attribution": {"repository": candidate["repository"],
                                      "repository_url": candidate["repository_url"],
                                      "license": candidate["license"], "recipe_id": candidate["recipe_id"]}}
        plan["status"] = result["status"]
        plan["result"] = result
        self.store.execute("UPDATE plans SET payload=?,status=? WHERE id=?",
                           (self.store.encode(plan), plan["status"], plan_id))
        self.store.commit()
        self.engine._log("repair", f"{result['status']} {plan['classification']['rule_id']} repair for {plan['target']}")
        return result