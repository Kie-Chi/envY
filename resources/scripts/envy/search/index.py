"""Exact registry identity index shared by search, add, and future frontends."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from envy.search.model import ProviderReport, SearchResult
from envy.sources.catalog import legacy_source_id
from envy.secure_io import ensure_private_directory


INDEX_SCHEMA_VERSION = 2
DEFAULT_TTL_SECONDS = 24 * 60 * 60
NEGATIVE_TTL_SECONDS = 5 * 60


def default_index_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME", "").strip()
    cache_root = Path(root).expanduser() if root else Path.home() / ".cache"
    return cache_root / "envy" / "registry" / "index-v2.sqlite3"


def canonical_provider(result: SearchResult) -> str:
    """Collapse manager-specific sources into the manifest ecosystem provider."""
    if result.ecosystem == "native":
        return "native"
    return result.ecosystem or result.source


@dataclass(frozen=True)
class IndexedResult:
    result: SearchResult
    fetched_at: int
    expires_at: int

    @property
    def stale(self) -> bool:
        return self.expires_at <= int(time.time())

    @property
    def age_seconds(self) -> int:
        return max(0, int(time.time()) - self.fetched_at)


class RegistryIndex:
    """Small SQLite index keyed by provider, kind, name, and canonical ref."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_index_path()

    def _connect(self) -> sqlite3.Connection:
        ensure_private_directory(self.path.parent)
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries (
                source_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                provider TEXT NOT NULL,
                source TEXT NOT NULL,
                ecosystem TEXT NOT NULL,
                kind TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                canonical_ref TEXT NOT NULL,
                version TEXT,
                payload_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (source_id, fingerprint, provider, kind, canonical_ref)
            );
            CREATE INDEX IF NOT EXISTS entries_name_idx
                ON entries(provider, kind, normalized_name);
            CREATE TABLE IF NOT EXISTS misses (
                source_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                provider TEXT NOT NULL,
                kind TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                checked_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                PRIMARY KEY (source_id, fingerprint, provider, kind, normalized_name)
            );
        """)
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema', ?)",
            (str(INDEX_SCHEMA_VERSION),),
        )
        connection.commit()
        return connection

    def _connect_readonly(self) -> sqlite3.Connection:
        """Open an existing index without schema, metadata, or journal writes."""
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=1,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _readonly_connection(self):
        connection = self._connect_readonly()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def put_results(
        self,
        results: Iterable[SearchResult],
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        now: int | None = None,
    ) -> int:
        timestamp = int(time.time()) if now is None else now
        rows = []
        for result in results:
            if not result.ref or not result.name or not result.ecosystem or not result.kind:
                continue
            rows.append((
                result.source_id or legacy_source_id(result.ecosystem, result.ref, result.kind) or result.ecosystem,
                result.source_fingerprint or "",
                canonical_provider(result),
                result.source,
                result.ecosystem,
                result.kind,
                result.name.casefold(),
                result.ref,
                result.version,
                json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp + ttl_seconds,
            ))
        if not rows:
            return 0
        with self._connection() as connection:
            connection.executemany("""
                INSERT INTO entries(
                    source_id, fingerprint, provider, source, ecosystem, kind, normalized_name,
                    canonical_ref, version, payload_json, fetched_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, fingerprint, provider, kind, canonical_ref) DO UPDATE SET
                    source=excluded.source,
                    ecosystem=excluded.ecosystem,
                    normalized_name=excluded.normalized_name,
                    version=excluded.version,
                    payload_json=excluded.payload_json,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at
            """, rows)
            connection.executemany(
                "DELETE FROM misses WHERE source_id=? AND fingerprint=? AND provider=? AND kind=? AND normalized_name=?",
                [(row[0], row[1], row[2], row[5], row[6]) for row in rows],
            )
        return len(rows)

    def put_reports(self, reports: Iterable[ProviderReport]) -> int:
        return self.put_results(
            result
            for report in reports
            if report.error is None
            for result in report.results
        )

    def lookup(
        self,
        provider: str,
        kind: str,
        value: str,
        *,
        allow_stale: bool = False,
        source_id: str | None = None,
        fingerprint: str | None = None,
    ) -> IndexedResult | None:
        if not self.path.exists():
            return None
        normalized = value.casefold()
        try:
            with self._readonly_connection() as connection:
                row = connection.execute("""
                    SELECT * FROM entries
                    WHERE provider=? AND kind=?
                      AND (normalized_name=? OR canonical_ref=?)
                      AND (? IS NULL OR source_id=?) AND (? IS NULL OR fingerprint=?)
                    ORDER BY CASE WHEN canonical_ref=? THEN 0 ELSE 1 END,
                             fetched_at DESC
                    LIMIT 2
                """, (provider, kind, normalized, value, source_id, source_id, fingerprint, fingerprint, value)).fetchall()
                row = row[0] if len(row) == 1 else None
        except (OSError, sqlite3.Error):
            return None
        if row is None:
            return None
        indexed = self._decode(row)
        if indexed is None or (indexed.stale and not allow_stale):
            return None
        return indexed

    def suggest(
        self,
        provider: str,
        kind: str,
        prefix: str,
        *,
        limit: int = 50,
        allow_stale: bool = False,
        source_id: str | None = None,
        fingerprint: str | None = None,
    ) -> list[IndexedResult]:
        """Return exact-index prefix candidates without creating an empty DB."""
        if not self.path.exists():
            return []
        now = int(time.time())
        freshness = "" if allow_stale else "AND expires_at > ?"
        escaped_prefix = (
            prefix.casefold()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        parameters: list[object] = [
            provider, kind, escaped_prefix + "%", escaped_prefix + "%",
            source_id, source_id, fingerprint, fingerprint,
        ]
        if not allow_stale:
            parameters.append(now)
        parameters.append(limit)
        try:
            with self._readonly_connection() as connection:
                rows = connection.execute(f"""
                    SELECT * FROM entries
                    WHERE provider=? AND kind=?
                      AND (
                        normalized_name LIKE ? ESCAPE '\\'
                        OR lower(canonical_ref) LIKE ? ESCAPE '\\'
                      )
                      AND (? IS NULL OR source_id=?) AND (? IS NULL OR fingerprint=?)
                      {freshness}
                    ORDER BY normalized_name, canonical_ref
                    LIMIT ?
                """, parameters).fetchall()
        except (OSError, sqlite3.Error):
            return []
        return [indexed for row in rows if (indexed := self._decode(row)) is not None]

    def put_miss(
        self,
        provider: str,
        kind: str,
        value: str,
        *,
        now: int | None = None,
        source_id: str | None = None,
        fingerprint: str = "",
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self._connection() as connection:
            connection.execute("""
                INSERT INTO misses(source_id, fingerprint, provider, kind, normalized_name, checked_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, fingerprint, provider, kind, normalized_name) DO UPDATE SET
                    checked_at=excluded.checked_at,
                    expires_at=excluded.expires_at
            """, (
                (source_id or legacy_source_id(provider) or provider), fingerprint, provider, kind, value.casefold(), timestamp,
                timestamp + NEGATIVE_TTL_SECONDS,
            ))

    def recently_missing(self, provider: str, kind: str, value: str, *, source_id: str | None = None, fingerprint: str = "") -> bool:
        if not self.path.exists():
            return False
        try:
            with self._readonly_connection() as connection:
                row = connection.execute("""
                    SELECT expires_at FROM misses
                    WHERE source_id=? AND fingerprint=? AND provider=? AND kind=? AND normalized_name=?
                """, ((source_id or legacy_source_id(provider) or provider), fingerprint, provider, kind, value.casefold())).fetchone()
        except (OSError, sqlite3.Error):
            return False
        return row is not None and int(row["expires_at"]) > int(time.time())

    def stats(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "path": str(self.path), "entries": 0, "fresh": 0,
                "stale": 0, "misses": 0, "providers": {},
            }
        now = int(time.time())
        with self._readonly_connection() as connection:
            entries = connection.execute("SELECT COUNT(*) AS count FROM entries").fetchone()["count"]
            fresh = connection.execute(
                "SELECT COUNT(*) AS count FROM entries WHERE expires_at > ?", (now,)
            ).fetchone()["count"]
            misses = connection.execute(
                "SELECT COUNT(*) AS count FROM misses WHERE expires_at > ?", (now,)
            ).fetchone()["count"]
            providers = {
                row["provider"]: row["count"]
                for row in connection.execute(
                    "SELECT provider, COUNT(*) AS count FROM entries GROUP BY provider"
                )
            }
        return {
            "path": str(self.path), "entries": entries, "fresh": fresh,
            "stale": entries - fresh, "misses": misses, "providers": providers,
        }

    def clear_source(self, source_id: str) -> None:
        if not self.path.exists():
            return
        with self._connection() as connection:
            connection.execute("DELETE FROM entries WHERE source_id=?", (source_id,))
            connection.execute("DELETE FROM misses WHERE source_id=?", (source_id,))

    def clear(self) -> bool:
        removed = False
        for path in (
            self.path,
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            try:
                path.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed

    @staticmethod
    def _decode(row: sqlite3.Row) -> IndexedResult | None:
        try:
            payload = json.loads(row["payload_json"])
            result = SearchResult.from_dict(payload)
            return IndexedResult(
                result=result,
                fetched_at=int(row["fetched_at"]),
                expires_at=int(row["expires_at"]),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
