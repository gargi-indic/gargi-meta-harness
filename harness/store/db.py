import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


MIGRATIONS = [
    """
    CREATE TABLE repos (
        full_name TEXT PRIMARY KEY,
        default_branch TEXT,
        config_json TEXT,
        synced_at TEXT
    );

    CREATE TABLE issues (
        repo TEXT,
        number INTEGER,
        id INTEGER,
        title TEXT,
        body TEXT,
        state TEXT,
        labels_json TEXT,
        parent INTEGER,
        blocked_by_json TEXT,
        updated_at TEXT,
        PRIMARY KEY (repo, number)
    );

    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        repo TEXT,
        issue INTEGER,
        playbook_id TEXT,
        playbook_sha TEXT,
        status TEXT,
        context_json TEXT,
        ledger_comment_id INTEGER,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE steps (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        node_id TEXT,
        visit INTEGER,
        status TEXT,
        outcome TEXT,
        ref_json TEXT,
        output_json TEXT,
        poll_after TEXT,
        started_at TEXT,
        ended_at TEXT,
        FOREIGN KEY (run_id) REFERENCES runs(id),
        UNIQUE (run_id, node_id, visit)
    );

    CREATE TABLE gates (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        step_id TEXT,
        name TEXT,
        status TEXT,
        note TEXT,
        decided_by TEXT,
        created_at TEXT,
        decided_at TEXT
    );

    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        repo TEXT,
        issue INTEGER,
        run_id TEXT,
        kind TEXT,
        data_json TEXT
    );

    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        repo TEXT,
        op TEXT,
        coalesce_key TEXT UNIQUE,
        payload_json TEXT,
        status TEXT,
        attempts INTEGER,
        last_error TEXT,
        not_before TEXT,
        created_at TEXT,
        sent_at TEXT
    );

    CREATE TABLE http_cache (
        url TEXT PRIMARY KEY,
        etag TEXT,
        body_json TEXT,
        fetched_at TEXT
    );
    """
]
SCHEMA_VERSION = len(MIGRATIONS)


class Store:
    def __init__(
        self,
        path: Path | str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = _dict_factory
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")

        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict[str, Any]]:
        return self.conn.execute(sql, params).fetchall()

    def migrate(self) -> None:
        rows = self.query("SELECT value FROM meta WHERE key = 'schema_version'")
        current_version = int(rows[0]["value"]) if rows else 0

        for idx, migration in enumerate(MIGRATIONS[current_version:]):
            version = current_version + idx + 1
            with self.tx():
                # Some statements in migration could be separated by semicolons
                for stmt in migration.split(";"):
                    if stmt.strip():
                        self.execute(stmt)
                self.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(version),),
                )
