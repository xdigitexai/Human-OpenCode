from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=10000;
            CREATE TABLE IF NOT EXISTS repositories (
                id INTEGER PRIMARY KEY, root TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                url TEXT, license TEXT, indexed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY, repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
                path TEXT NOT NULL, language TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                line INTEGER NOT NULL, end_line INTEGER NOT NULL, source TEXT NOT NULL, imports TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS symbols_language ON symbols(language);
            CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS checkpoints (
                id TEXT PRIMARY KEY, root TEXT NOT NULL, head TEXT, created_at TEXT NOT NULL,
                files_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, category TEXT NOT NULL,
                message TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS desktop_tasks (
                id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL,
                step INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
                action TEXT NOT NULL DEFAULT '', current_path TEXT,
                payload TEXT NOT NULL, checkpoint TEXT NOT NULL,
                file_verified INTEGER, command_approved INTEGER, command_result TEXT,
                stop_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS desktop_tasks_status ON desktop_tasks(status, created_at);
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id TEXT PRIMARY KEY, source_root TEXT NOT NULL, workspace TEXT NOT NULL,
                task TEXT NOT NULL, status TEXT NOT NULL, control TEXT NOT NULL,
                iteration INTEGER NOT NULL DEFAULT 0, max_iterations INTEGER NOT NULL,
                duration_seconds INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                state_json TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_sessions_status ON agent_sessions(status, created_at);
            CREATE TABLE IF NOT EXISTS agent_journal (
                id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL, category TEXT NOT NULL, message TEXT NOT NULL,
                details_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS agent_journal_session ON agent_journal(session_id, id);
            CREATE TABLE IF NOT EXISTS verified_repairs (
                id TEXT PRIMARY KEY, rule_id TEXT NOT NULL, language TEXT NOT NULL,
                target_glob TEXT NOT NULL, operation TEXT NOT NULL, before_text TEXT,
                after_text TEXT, repository TEXT NOT NULL, repository_url TEXT,
                license TEXT NOT NULL, verified_at TEXT NOT NULL,
                UNIQUE(rule_id,target_glob,operation,before_text,after_text,repository)
            );
            CREATE INDEX IF NOT EXISTS verified_repairs_signature ON verified_repairs(rule_id,language);
        """)
        self.connection.commit()

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.connection.execute(query, parameters)

    def commit(self) -> None:
        with self.lock:
            self.connection.commit()

    def close(self) -> None:
        with self.lock:
            self.connection.close()

    @staticmethod
    def encode(value: Any) -> str:
        return json.dumps(value, ensure_ascii=True)
