import json
from typing import Any

from harness.github.client import EtagCache
from harness.store.db import Store


class SqliteEtagCache(EtagCache):
    def __init__(self, store: Store) -> None:
        self.store = store

    def get(self, url: str) -> tuple[str, Any] | None:
        rows = self.store.query("SELECT etag, body_json FROM http_cache WHERE url = ?", (url,))
        if not rows:
            return None
        r = rows[0]
        return r["etag"], json.loads(r["body_json"])

    def put(self, url: str, etag: str, body: Any) -> None:
        now = self.store.now()
        body_json = json.dumps(body)
        self.store.execute(
            """
            INSERT INTO http_cache (url, etag, body_json, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                etag = excluded.etag,
                body_json = excluded.body_json,
                fetched_at = excluded.fetched_at
            """,
            (url, etag, body_json, now),
        )
