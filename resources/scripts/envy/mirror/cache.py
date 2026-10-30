"""SQLite cache for mirror source lists and speed measurements."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


def mirror_cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(root).expanduser() if root else Path.home() / ".cache"
    return base / "envy" / "mirrors" / "index-v1.sqlite3"


class MirrorCache:
    """Private cache for chsrc candidates and slow target measurements."""

    def __init__(self, path: Path | None = None):
        self.path = path or mirror_cache_path()

    def _connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sources (
                target TEXT NOT NULL,
                code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(target, code)
            );
            CREATE TABLE IF NOT EXISTS measurements (
                target TEXT NOT NULL,
                code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY(target, code)
            );
        """)
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema', '1')"
        )
        connection.commit()
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any] | None:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["fetchedAt"] = int(row["fetched_at"])
        payload["expiresAt"] = int(row["expires_at"])
        payload["stale"] = int(row["expires_at"]) <= int(time.time())
        return payload

    def get(self, table: str, target: str, *, allow_stale: bool = False) -> list[dict[str, Any]]:
        if table not in {"sources", "measurements"} or not self.path.exists():
            return []
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=1)
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE target=? ORDER BY code", (target,)
            ).fetchall()
        except (OSError, sqlite3.Error):
            return []
        finally:
            if connection is not None:
                connection.close()
        values = [decoded for row in rows if (decoded := self._decode(row)) is not None]
        if not allow_stale:
            values = [value for value in values if not value["stale"]]
        return values

    def put(self, table: str, target: str, values: list[dict[str, Any]], ttl: int) -> None:
        if table not in {"sources", "measurements"}:
            raise ValueError(f"invalid mirror cache table: {table}")
        now = int(time.time())
        try:
            connection = self._connection()
        except (OSError, sqlite3.Error):
            # Cache is an optimization; an unwritable home/cache must not make
            # source discovery or TUI selection fail.
            return
        try:
            connection.execute(f"DELETE FROM {table} WHERE target=?", (target,))
            connection.executemany(
                f"INSERT INTO {table}(target, code, payload_json, fetched_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (target, str(value.get("code", "")), json.dumps(value, ensure_ascii=False, sort_keys=True), now, now + ttl)
                    for value in values if value.get("code")
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def stats(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"path": str(self.path), "sources": 0, "measurements": 0, "stale": 0}
        try:
            with sqlite3.connect(self.path) as connection:
                sources = connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
                measurements = connection.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
                stale = connection.execute(
                    "SELECT COUNT(*) FROM measurements WHERE expires_at <= ?", (int(time.time()),)
                ).fetchone()[0]
        except (OSError, sqlite3.Error):
            return {"path": str(self.path), "sources": 0, "measurements": 0, "stale": 0}
        return {"path": str(self.path), "sources": sources, "measurements": measurements, "stale": stale}

    def clear(self) -> bool:
        if not self.path.exists():
            return False
        try:
            self.path.unlink()
            return True
        except OSError:
            return False
