from __future__ import annotations

import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .desktop import DesktopController
from .languages import ADAPTERS, adapter_for_path, available_commands
from .store import Store

IGNORED_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "vendor", "target",
                "dist", "build", "__pycache__", ".next", ".opencode-human-bot"}
IGNORED_FILES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519"}
MAX_SOURCE_BYTES = 1_000_000
SAFE_COMMAND_ENV = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "USERPROFILE", "HOME"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_path(root: Path, relative: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Path escapes the project root")
    return candidate


def _run(args: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_COMMAND_ENV}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_LFS_SKIP_SMUDGE": "1"})
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout,
                          check=False, env=env)


class CodingEngine:
    def __init__(self, root: Path, database: Path | None = None):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise ValueError(f"Project root is not a directory: {self.root}")
        self.store = Store(database or self.root / ".opencode-human-bot" / "index.sqlite3")
        self.cache = self.root / ".opencode-human-bot" / "repositories"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.desktop = DesktopController(self)
        from .browser import BrowserAutomation
        from .repairs import RepairEngine

        self.repairs = RepairEngine(self)
        self.browser = BrowserAutomation(self)

    def _log(self, category: str, message: str) -> None:
        self.store.execute("INSERT INTO events(created_at,category,message) VALUES(?,?,?)",
                           (utc_now(), category, message[:1000]))
        self.store.commit()

    def _files(self, root: Path):
        for directory, names, filenames in os.walk(root):
            names[:] = [name for name in names if name not in IGNORED_DIRS]
            base = Path(directory)
            for name in filenames:
                if name in IGNORED_FILES or name.startswith(".env."):
                    continue
                path = base / name
                try:
                    resolved = path.resolve()
                    if (not path.is_symlink() and (resolved == root.resolve() or root.resolve() in resolved.parents)
                            and path.stat().st_size <= MAX_SOURCE_BYTES):
                        yield path
                except OSError:
                    continue

    def inspect(self) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        filenames: set[str] = set()
        for path in self._files(self.root):
            filenames.add(path.name)
            adapter = adapter_for_path(path)
            if adapter:
                counts[adapter.name] += 1
        text = self._manifest_text().lower()
        framework_rules = {
            "FastAPI": "fastapi" in text,
            "Django": "django" in text,
            "Flask": "flask" in text,
            "Next.js": any(name in filenames for name in ("next.config.js", "next.config.ts", "next.config.mjs", "next.config.mts")),
            "React": '"react"' in text or "'react'" in text,
            "Laravel": '"laravel/framework"' in text,
            "Symfony": '"symfony/' in text,
        }
        languages = [{"name": adapter.name, "files": counts[adapter.name],
                      "parser_available": adapter.parser_available,
                      "parser_status": "available" if adapter.parser_available else "not_installed",
                  "compiler": list(adapter.compiler), "tests": list(adapter.tests),
                  "available_commands": available_commands(adapter)}
                     for adapter in ADAPTERS if counts[adapter.name]]
        manifests = sorted(name for name in filenames if name in {
            "pyproject.toml", "requirements.txt", "Pipfile", "setup.py", "package.json",
            "composer.json", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
            "build.gradle.kts", "Gemfile", "Package.swift", "CMakeLists.txt", "Makefile",
        } or name.endswith((".csproj", ".sln")))
        return {"root": str(self.root), "languages": languages,
                "frameworks": [name for name, present in framework_rules.items() if present],
                "dependency_manifests": manifests,
                "tree_sitter_installed": importlib.util.find_spec("tree_sitter_language_pack") is not None}

    def _manifest_text(self) -> str:
        chunks = []
        for path in self.root.iterdir():
            if path.is_file() and (path.name in {"pyproject.toml", "requirements.txt", "package.json", "composer.json", "Cargo.toml", "go.mod", "Gemfile"}):
                if path.is_symlink():
                    continue
                try:
                    chunks.append(path.read_text(encoding="utf-8", errors="replace")[:200_000])
                except OSError:
                    continue
        return "\n".join(chunks)

    def add_local_repository(self, path: Path) -> dict[str, Any]:
        repository_root = path.resolve()
        if not repository_root.is_dir():
            raise ValueError("Repository folder does not exist")
        return self._register(repository_root, None)

    def search_github(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        params = urllib.parse.urlencode({"q": query, "sort": "stars", "order": "desc",
                                         "per_page": min(max(limit, 1), 30)})
        request = urllib.request.Request(
            f"https://api.github.com/search/repositories?{params}",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "OpenCode-Human-Bot"})
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
        return [{"url": row["clone_url"], "name": row["full_name"],
                 "description": row.get("description"),
                 "license": (row.get("license") or {}).get("spdx_id"),
                 "stars": row.get("stargazers_count", 0)} for row in payload.get("items", [])]

    def clone_repository(self, url: str, approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Cloning requires explicit user approval")
        if not re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", url):
            raise ValueError("Only public HTTPS GitHub repository URLs are accepted")
        slug = url.removesuffix(".git").rstrip("/").split("/")[-1]
        destination = self.cache / f"{slug}-{hashlib.sha256(url.encode()).hexdigest()[:8]}"
        if destination.exists():
            return self._register(destination, url)
        result = _run(["git", "clone", "--depth", "1", "--no-recurse-submodules", "--",
                   url, str(destination)], self.root, timeout=300)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:] or "git clone failed")
        self._log("repository", f"Cloned public repository {url}")
        return self._register(destination, url)

    def _register(self, root: Path, url: str | None) -> dict[str, Any]:
        license_text = None
        try:
            for path in root.iterdir():
                if path.is_file() and not path.is_symlink() and path.name.lower().startswith(("license", "copying")):
                    license_text = path.read_text(encoding="utf-8", errors="replace")[:8000].strip()
                    break
        except OSError:
            pass
        self.store.execute("INSERT INTO repositories(root,name,url,license,indexed_at) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(root) DO UPDATE SET url=excluded.url, license=excluded.license, indexed_at=excluded.indexed_at",
                           (str(root), root.name, url, license_text, utc_now()))
        self.store.commit()
        row = self.store.execute("SELECT id FROM repositories WHERE root=?", (str(root),)).fetchone()
        return {"id": row["id"], "root": str(root), "name": root.name,
                "url": url, "license": license_text, "licensed": bool(license_text)}

    def index_repository(self, repository: Path | None = None) -> dict[str, Any]:
        root = (repository or self.root).resolve()
        if not root.is_dir():
            raise ValueError("Repository folder does not exist")
        row = self.store.execute("SELECT * FROM repositories WHERE root=?", (str(root),)).fetchone()
        if row is None:
            metadata = self._register(root, None)
            row = self.store.execute("SELECT * FROM repositories WHERE id=?", (metadata["id"],)).fetchone()
        self.store.execute("DELETE FROM symbols WHERE repository_id=?", (row["id"],))
        file_count = symbol_count = parse_failures = 0
        for path in self._files(root):
            adapter = adapter_for_path(path)
            if not adapter:
                continue
            try:
                source_bytes = path.read_bytes()
                source = source_bytes.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            file_count += 1
            structures, error = adapter.parse(source_bytes)
            parse_failures += error is not None
            for symbol in structures:
                self.store.execute("INSERT INTO symbols(repository_id,path,language,kind,name,line,end_line,source,imports) "
                                   "VALUES(?,?,?,?,?,?,?,?,?)",
                                   (row["id"], path.relative_to(root).as_posix(), adapter.name,
                                    symbol["kind"], symbol["name"], symbol["line"], symbol["end_line"],
                                    symbol["source"], self.store.encode(adapter.imports(source))))
                symbol_count += 1
        self.store.execute("UPDATE repositories SET indexed_at=? WHERE id=?", (utc_now(), row["id"]))
        self.store.commit()
        self._log("index", f"Indexed {file_count} source files from {root}")
        return {"repository": str(root), "files": file_count, "symbols": symbol_count,
                "parse_failures": parse_failures, "license": row["license"],
                "parser_available": any(a.parser_available for a in ADAPTERS)}

    def _compatibility(self, source_language: str) -> dict[str, Any]:
        target_languages = {item["name"] for item in self.inspect()["languages"]}
        language_match = source_language in target_languages
        return {"language_match": language_match, "target_languages": sorted(target_languages),
                "dependency_versions_verified": False,
                "note": "Dependency and ABI versions require human review; no version is inferred."}

    def search(self, query: str, language: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        terms = [term.lower() for term in re.findall(r"[\w.-]+", query) if term]
        if not terms:
            return []
        rows = self.store.execute("SELECT s.*,r.root,r.name AS repository,r.url,r.license FROM symbols s "
                                  "JOIN repositories r ON r.id=s.repository_id "
                                  "WHERE (? IS NULL OR lower(s.language)=lower(?))", (language, language)).fetchall()
        results = []
        for row in rows:
            searchable = f"{row['name']} {row['kind']} {row['path']} {row['source']}".lower()
            score = sum(3 if term == row["name"].lower() else 2 if term in row["path"].lower() else 1
                        for term in terms if term in searchable)
            if score:
                compatibility = self._compatibility(row["language"])
                results.append({"score": score, "name": row["name"], "kind": row["kind"],
                                "language": row["language"], "path": row["path"], "line": row["line"],
                                "source": row["source"], "imports": json.loads(row["imports"]),
                                "repository": row["repository"], "repository_url": row["url"],
                                "license": row["license"], "licensed": bool(row["license"]),
                                "compatibility": compatibility})
        return sorted(results, key=lambda item: (-item["score"], item["repository"], item["path"]))[:limit]

    def search_text(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        if not query:
            return []
        results = []
        repositories = self.store.execute("SELECT root,name,url,license FROM repositories ORDER BY name").fetchall()
        rg = shutil.which("rg")
        for repository in repositories:
            root = Path(repository["root"])
            if not root.is_dir():
                continue
            if rg:
                command = [rg, "--json", "--fixed-strings", "--hidden", "--glob", "!.git/**",
                           "--glob", "!.opencode-human-bot/**", "--glob", "!.env*", "--", query, "."]
                completed = subprocess.run(command, cwd=root, text=True, capture_output=True,
                                            timeout=30, check=False,
                                            env={key: value for key, value in os.environ.items()
                                                 if key.upper() in SAFE_COMMAND_ENV})
                for line in completed.stdout.splitlines():
                    try:
                        item = json.loads(line)
                        if item.get("type") != "match":
                            continue
                        data = item["data"]
                        relative_path = data["path"].get("text", "").replace("\\", "/").removeprefix("./")
                        results.append({"repository": repository["name"], "repository_url": repository["url"],
                                        "license": repository["license"],
                                        "path": relative_path,
                                        "line": data["line_number"], "text": data["lines"].get("text", "").rstrip()})
                    except (KeyError, json.JSONDecodeError):
                        continue
                    if len(results) >= limit:
                        return results[:limit]
            else:
                for path in self._files(root):
                    try:
                        lines = path.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeDecodeError):
                        continue
                    for line_number, line in enumerate(lines, 1):
                        if query in line:
                            results.append({"repository": repository["name"], "repository_url": repository["url"],
                                            "license": repository["license"],
                                            "path": path.relative_to(root).as_posix(),
                                            "line": line_number, "text": line})
                            if len(results) >= limit:
                                return results
        return results

    def create_plan(self, task: str, limit: int = 5) -> dict[str, Any]:
        if not task.strip():
            raise ValueError("Task must not be empty")
        plan_id = uuid.uuid4().hex
        candidates = self.search(task, limit=limit)
        usable = [candidate for candidate in candidates
                  if candidate["licensed"] and candidate["compatibility"]["language_match"]]
        plan = {"id": plan_id, "task": task, "project": self.inspect(), "candidates": usable,
                "changes": [], "status": "needs_source" if not usable else "awaiting_approval",
                "notice": ("No licensed same-language implementation was found; no code was generated."
                           if not usable else
                           "Review source attribution, license terms, and dependency compatibility before approval.")}
        self.store.execute("INSERT INTO plans(id,payload,status) VALUES(?,?,?)",
                           (plan_id, self.store.encode(plan), plan["status"]))
        self.store.commit()
        self._log("plan", f"Created plan {plan_id}: {task}")
        return plan

    def add_exact_replacement(self, plan_id: str, candidate_index: int, relative_path: str,
                              old: str, new: str) -> dict[str, Any]:
        row = self.store.execute("SELECT payload,status FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None or row["status"] != "awaiting_approval":
            raise ValueError("Plan is missing or no longer editable")
        plan = json.loads(row["payload"])
        if candidate_index < 0 or candidate_index >= len(plan["candidates"]):
            raise ValueError("Choose a candidate shown in this plan")
        candidate = plan["candidates"][candidate_index]
        if not candidate["licensed"] or not candidate["compatibility"]["language_match"]:
            raise ValueError("Unlicensed or cross-language source cannot be added to a plan")
        path = safe_path(self.root, relative_path)
        original = path.read_text(encoding="utf-8") if path.exists() else ""
        if not old or original.count(old) != 1:
            raise ValueError("The source anchor must occur exactly once in the target file")
        if any(change["path"] == relative_path for change in plan["changes"]):
            raise ValueError("A plan may contain only one change per file")
        updated = original.replace(old, new, 1)
        attribution = {"repository": candidate["repository"], "repository_url": candidate["repository_url"],
                       "source_path": candidate["path"], "license": candidate["license"]}
        plan["changes"].append({"path": relative_path, "before": original, "after": updated,
                                "diff": "".join(difflib.unified_diff(original.splitlines(True), updated.splitlines(True),
                                                                       fromfile=relative_path, tofile=relative_path)),
                                "attribution": attribution})
        self.store.execute("UPDATE plans SET payload=? WHERE id=?", (self.store.encode(plan), plan_id))
        self.store.commit()
        return plan

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.store.execute("SELECT payload FROM plans WHERE id=?", (plan_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown plan")
        return json.loads(row["payload"])

    def checkpoint(self, files: list[str]) -> dict[str, Any]:
        git = _run(["git", "rev-parse", "HEAD"], self.root)
        head = git.stdout.strip() if git.returncode == 0 else None
        snapshots = {}
        for relative in files:
            path = safe_path(self.root, relative)
            snapshots[relative] = path.read_text(encoding="utf-8") if path.exists() else None
        checkpoint_id = uuid.uuid4().hex
        self.store.execute("INSERT INTO checkpoints(id,root,head,created_at,files_json) VALUES(?,?,?,?,?)",
                           (checkpoint_id, str(self.root), head, utc_now(), self.store.encode(snapshots)))
        self.store.commit()
        ref_created = bool(head and _run(["git", "update-ref", f"refs/opencode-human-bot/{checkpoint_id}", head], self.root).returncode == 0)
        self._log("checkpoint", f"Created checkpoint {checkpoint_id} for {len(files)} file(s)")
        return {"id": checkpoint_id, "head": head, "git_ref_created": ref_created,
                "snapshotted_files": list(snapshots)}

    def apply_plan(self, plan_id: str, approved: bool = False, mode: str = "native") -> dict[str, Any]:
        if not approved:
            raise PermissionError("Applying changes requires explicit user approval")
        if mode != "native":
            raise ValueError("Physical edits must use the persistent, guarded desktop controller")
        prepared = self.prepare_apply(plan_id, approved=True)
        plan = self.get_plan(plan_id)
        checkpoint = prepared["checkpoint"]
        applied = []
        try:
            for change in plan["changes"]:
                target = safe_path(self.root, change["path"])
                current = target.read_text(encoding="utf-8") if target.exists() else ""
                if current != change["before"]:
                    raise RuntimeError(f"Target changed after planning: {change['path']}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(change["after"], encoding="utf-8", newline="")
                applied.append(change["path"])
        except Exception:
            self.abort_apply(plan_id)
            raise
        self.complete_apply(plan_id)
        self._log("apply", f"Applied plan {plan_id} to {len(applied)} file(s) in {mode} mode")
        return {"plan": plan_id, "files": applied, "checkpoint": checkpoint, "mode": "native"}

    def prepare_apply(self, plan_id: str, approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Applying changes requires explicit user approval")
        plan = self.get_plan(plan_id)
        if plan["status"] != "awaiting_approval" or not plan["changes"]:
            raise ValueError("Plan has no proposed changes to apply")
        for change in plan["changes"]:
            target = safe_path(self.root, change["path"])
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != change["before"]:
                raise RuntimeError(f"Target changed after planning: {change['path']}")
        checkpoint = self.checkpoint([change["path"] for change in plan["changes"]])
        plan["status"] = "applying"
        plan["checkpoint"] = checkpoint
        self.store.execute("UPDATE plans SET payload=?,status='applying' WHERE id=?",
                           (self.store.encode(plan), plan_id))
        self.store.commit()
        return {"plan": plan_id, "changes": plan["changes"], "checkpoint": checkpoint}

    def complete_apply(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["status"] != "applying":
            raise ValueError("Plan is not in the applying state")
        plan["status"] = "applied"
        self.store.execute("UPDATE plans SET payload=?,status='applied' WHERE id=?",
                           (self.store.encode(plan), plan_id))
        self.store.commit()
        self._log("apply", f"Completed plan {plan_id}")
        return {"plan": plan_id, "status": "applied", "checkpoint": plan["checkpoint"]}

    def abort_apply(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["status"] != "applying":
            raise ValueError("Plan is not in the applying state")
        rollback = self.rollback(plan["checkpoint"]["id"], approved=True)
        plan["status"] = "awaiting_approval"
        plan.pop("checkpoint", None)
        self.store.execute("UPDATE plans SET payload=?,status='awaiting_approval' WHERE id=?",
                           (self.store.encode(plan), plan_id))
        self.store.commit()
        return rollback

    def rollback(self, checkpoint_id: str, approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Restoring a checkpoint requires explicit user approval")
        row = self.store.execute("SELECT * FROM checkpoints WHERE id=? AND root=?",
                                 (checkpoint_id, str(self.root))).fetchone()
        if row is None:
            raise ValueError("Unknown checkpoint")
        restored = []
        for relative, content in json.loads(row["files_json"]).items():
            path = safe_path(self.root, relative)
            if content is None:
                if path.exists():
                    path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8", newline="")
            restored.append(relative)
        self._log("rollback", f"Restored checkpoint {checkpoint_id}")
        return {"checkpoint": checkpoint_id, "restored": restored, "git_head": row["head"]}

    def run_command(self, command: list[str], approved: bool = False, timeout: int = 300) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Terminal commands require explicit user approval")
        if not command or any("\x00" in part for part in command):
            raise ValueError("Invalid command")
        safe_env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_COMMAND_ENV}
        result = subprocess.run(command, cwd=self.root, text=True, capture_output=True,
                                timeout=min(max(timeout, 1), 900), check=False,
                                env={**safe_env, "GIT_TERMINAL_PROMPT": "0"})
        secret_values = [value for key, value in os.environ.items()
                         if any(token in key.lower() for token in ("token", "secret", "password", "api_key", "apikey"))
                         and len(value) >= 6]

        def redact(value: str) -> str:
            for secret in secret_values:
                value = value.replace(secret, "[REDACTED]")
            return re.sub(r"(?i)(token|secret|password|api[_-]?key)(\s*[=:]\s*)[^\s]+",
                          r"\1\2[REDACTED]", value)

        self._log("command", f"Ran approved command: {command[0]} (exit {result.returncode})")
        return {"command": [redact(part) for part in command], "returncode": result.returncode,
                "stdout": redact(result.stdout)[-12000:], "stderr": redact(result.stderr)[-12000:]}

    def capture_screenshot(self, approved: bool = False) -> dict[str, Any]:
        if not approved:
            raise PermissionError("Desktop screenshots require explicit user approval")
        try:
            pyautogui = __import__("pyautogui")
        except ImportError as error:
            raise RuntimeError("Screenshots require the optional PyAutoGUI dependency") from error
        directory = self.root / ".opencode-human-bot" / "screenshots"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.png"
        pyautogui.screenshot().save(path)
        self._log("screenshot", f"Captured user-approved desktop screenshot at {path}")
        return {"path": str(path), "relative_path": path.relative_to(self.root).as_posix()}

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        return [dict(row) for row in self.store.execute(
            "SELECT id,created_at,category,message FROM events ORDER BY id DESC LIMIT ?",
            (min(max(limit, 1), 500),)).fetchall()]

    def close(self) -> None:
        self.desktop.close()
        self.store.close()