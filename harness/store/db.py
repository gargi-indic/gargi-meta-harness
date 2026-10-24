import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


MIGRATIONS: list[list[str]] = [
    [
        """
        CREATE TABLE repos (
            full_name TEXT PRIMARY KEY,
            default_branch TEXT,
            config_json TEXT,
            synced_at TEXT
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
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
        )
        """,
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            repo TEXT,
            issue INTEGER,
            run_id TEXT,
            kind TEXT,
            data_json TEXT
        )
        """,
        """
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
        )
        """,
        """
        CREATE TABLE http_cache (
            url TEXT PRIMARY KEY,
            etag TEXT,
            body_json TEXT,
            fetched_at TEXT
        )
        """,
    ],
    [
        """
        CREATE TABLE control_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            step_id TEXT,
            kind TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            handled_at TEXT,
            result_json TEXT
        )
        """,
        "CREATE INDEX idx_control_requests_pending ON control_requests (handled_at, id)",
    ],
    [
        # A mirror of the proposal comments on the control issue (GitHub is the store; `harness sync` refills it).
        """
        CREATE TABLE proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            plan_sha TEXT NOT NULL,
            json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (run_id, plan_sha)
        )
        """,
    ],
    [
        # Detailed spend rows. A MIRROR: the per-run totals also travel in the run's ledger snapshot
        # (`usage`), and `harness sync` refills this table from it (GitHub is the store).
        """
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            step_id TEXT,
            source TEXT NOT NULL,
            adapter TEXT NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cost_usd REAL NOT NULL,
            estimated INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_usage_run ON usage (run_id)",
        "CREATE INDEX idx_usage_created ON usage (created_at)",
    ],
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
        self._tx_depth = 0

    def close(self) -> None:
        self.conn.close()

    def now(self) -> str:
        return self.clock().replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    @contextmanager
    def tx(self) -> Iterator[None]:
        if self._tx_depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute(f"SAVEPOINT sp_{self._tx_depth}")
        self._tx_depth += 1
        try:
            yield
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute(f"RELEASE sp_{self._tx_depth}")
        except BaseException:
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute(f"ROLLBACK TO sp_{self._tx_depth}")
                self.conn.execute(f"RELEASE sp_{self._tx_depth}")
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
                for stmt in migration:
                    if stmt.strip():
                        self.execute(stmt)
                self.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (str(version),),
                )
