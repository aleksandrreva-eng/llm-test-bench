"""Database management for LLM Test Bench.
Handles SQLite operations for storing test runs and case results."""

import sqlite3
import json
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import contextmanager
from typing import Any


@dataclass
class DBRun:
    """Representation of a TestRun in the database."""

    run_id: str
    model: str
    set_id: str
    set_name: str
    started: str
    finished: str | None
    status: str
    total: int
    passed: int
    failed: int
    scored: float | None
    duration: float
    params: dict[str, Any] = field(default_factory=dict)

    def to_tuple(self) -> tuple:
        return (
            self.run_id,
            self.model,
            self.set_id,
            self.set_name,
            self.started,
            self.finished,
            self.status,
            self.total,
            self.passed,
            self.failed,
            self.scored,
            self.duration,
            json.dumps(self.params, ensure_ascii=False),
        )


@dataclass
class DBCaseResult:
    """Representation of a CaseResult in the database."""

    run_id: str
    case_id: str
    name: str
    prompt: str
    answer: str
    reasoning: str
    prompt_tokens: int
    completion_tokens: int
    ttft_ms: float
    total_ms: float
    passed: bool
    reason: str
    check_type: str
    judge_score: float | None
    judge_response: str | None
    judge_error: str | None
    extra: dict[str, Any]

    def to_tuple(self) -> tuple:
        return (
            self.run_id,
            self.case_id,
            self.name,
            self.prompt,
            self.answer,
            self.reasoning,
            self.prompt_tokens,
            self.completion_tokens,
            self.ttft_ms,
            self.total_ms,
            1 if self.passed else 0,
            self.reason,
            self.check_type,
            self.judge_score,
            self.judge_response,
            self.judge_error,
            json.dumps(self.extra, ensure_ascii=False),
        )


class DatabaseManager:
    """Manages connection and execution of SQL commands."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        """Creates tables if they don't exist."""
        with self.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    model TEXT NOT NULL,
                    set_id TEXT NOT NULL,
                    set_name TEXT,
                    started TEXT NOT NULL,
                    finished TEXT,
                    status TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    passed INTEGER NOT NULL,
                    failed INTEGER NOT NULL,
                    scored REAL,
                    duration REAL,
                    params TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS case_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    name TEXT,
                    prompt TEXT,
                    answer TEXT,
                    reasoning TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    ttft_ms REAL,
                    total_ms REAL,
                    passed INTEGER,
                    reason TEXT,
                    check_type TEXT,
                    judge_score REAL,
                    judge_response TEXT,
                    judge_error TEXT,
                    extra TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id)
                )
            """)
            # Index for faster filtering
            conn.execute("CREATE INDEX IF NOT EXISTS idx_case_run_id ON case_results(run_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_set_id ON runs(set_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_run_model ON runs(model)")

    @contextmanager
    def connection(self):
        """Context manager for SQLite connection.

        Фиксирует транзакцию при нормальном выходе и откатывает при
        исключении. Без commit записи теряются при закрытии соединения —
        именно это ломало сохранение прогона в историю.
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def save_run(self, run_data: DBRun, case_results: list[DBCaseResult]) -> None:
        """Saves a run and its cases in a single transaction."""
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, model, set_id, set_name, started, finished, status, 
                    total, passed, failed, scored, duration, params
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                run_data.to_tuple(),
            )
            if case_results:
                conn.executemany(
                    """
                    INSERT INTO case_results (
                        run_id, case_id, name, prompt, answer, reasoning, 
                        prompt_tokens, completion_tokens, ttft_ms, total_ms, 
                        passed, reason, check_type, judge_score, judge_response, 
                        judge_error, extra
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [c.to_tuple() for c in case_results],
                )

    def get_all_runs(self, set_id: str | None = None) -> list[dict[str, Any]]:
        """Returns all runs, optionally filtered by set_id."""
        query = "SELECT * FROM runs"
        params = []
        if set_id:
            query += " WHERE set_id = ?"
            params.append(set_id)
        query += " ORDER BY started DESC"

        # params должен быть не пустым, когда в query есть «?», иначе
        # sqlite3 ругается «incorrect number of bindings supplied».
        with self.connection() as conn:
            cursor = conn.execute(query, tuple(params))
            return [dict(row) for row in cursor.fetchall()]

    def get_run_details(self, run_id: str) -> dict[str, Any] | None:
        """Gets detailed run info including its cases."""
        with self.connection() as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run:
                return None

            cases = conn.execute(
                "SELECT * FROM case_results WHERE run_id = ?", (run_id,)
            ).fetchall()

            result = dict(run)
            result["cases"] = [dict(c) for c in cases]
            # Parse JSON params back
            if "params" in result and result["params"]:
                result["params"] = json.loads(result["params"])
            return result

    def delete_run(self, run_id: str) -> None:
        """Deletes a run and all its case results."""
        with self.connection() as conn:
            conn.execute("DELETE FROM case_results WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))

    def query_runs(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Direct SQL access for complex reporting."""
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, params)]
