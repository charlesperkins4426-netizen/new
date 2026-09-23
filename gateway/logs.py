from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .errors import GatewayError


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RequestLogs:
    """Small bounded SQLite log store. Every database operation runs off-loop."""

    def __init__(self, directory: Path, secrets: Callable[[], list[str]], retention: int):
        self.path = directory / "requests.sqlite3"
        self.secrets = secrets
        self.retention = retention
        self.lock = threading.RLock()

    def redact(self, value, extra_secrets=()):
        if isinstance(value, dict):
            return {k: self.redact(v, extra_secrets) for k, v in value.items()}
        if isinstance(value, list):
            return [self.redact(v, extra_secrets) for v in value]
        if not isinstance(value, str):
            return value
        for secret in sorted(set(self.secrets()) | set(extra_secrets), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[已脱敏]")
        value = re.sub(r"(?i)((?:__Secure-)?next-auth\.session-token(?:\.\d+)?=)[^;\s]+", r"\1[已脱敏]", value)
        return re.sub(
            r"(?im)(authorization\s*:\s*|(?:set-)?cookie\s*:\s*)[^\r\n]+",
            r"\1[已脱敏]", value,
        )

    async def _run(self, operation):
        def execute():
            with self.lock:
                try:
                    with sqlite3.connect(self.path, timeout=5) as db:
                        db.row_factory = sqlite3.Row
                        return operation(db)
                except (sqlite3.Error, OSError):
                    raise GatewayError(507, "logs_unavailable", "请求日志无法写入或读取，请检查磁盘。") from None
        return await asyncio.to_thread(execute)

    async def initialize(self):
        def prepare():
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(self.path, 0o600)
        try:
            await asyncio.to_thread(prepare)
        except OSError:
            raise GatewayError(507, "logs_unavailable", "无法创建请求日志，请检查数据目录。") from None

        def initialize(db):
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, account_id TEXT,
                model TEXT, status TEXT NOT NULL, ttft_ms REAL, duration_ms REAL,
                error TEXT, code TEXT, messages TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
                reasoning_content TEXT NOT NULL DEFAULT '')""")
            db.execute("CREATE INDEX IF NOT EXISTS requests_time ON requests(started_at)")
            db.execute("""UPDATE requests SET status='interrupted', code='process_interrupted',
                error='服务重启前请求没有正常结束。' WHERE status='running'""")
            self._prune(db)
        await self._run(initialize)

    def _prune(self, db):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention)).isoformat()
        db.execute("DELETE FROM requests WHERE started_at < ? AND status != 'running'", (cutoff,))

    async def start(self, request_id: str, started_at: str, model: str | None, messages: list):
        safe_messages = json.dumps(self.redact(messages), ensure_ascii=False)
        def insert(db):
            self._prune(db)
            db.execute(
                "INSERT INTO requests(id,started_at,model,status,messages) VALUES(?,?,?,'running',?)",
                (request_id, started_at, self.redact(model), safe_messages),
            )
        await self._run(insert)

    async def bind(self, request_id: str, account_id: str, model: str):
        await self._run(lambda db: db.execute(
            "UPDATE requests SET account_id=?,model=? WHERE id=?",
            (account_id, self.redact(model), request_id),
        ).rowcount)

    async def finish(self, request_id: str, *, status: str, ttft_ms: float | None,
                     duration_ms: float, content: str, reasoning_content: str,
                     error: str | None = None, code: str | None = None, extra_secrets=()):
        values = [status, ttft_ms, duration_ms, self.redact(content, extra_secrets), self.redact(reasoning_content, extra_secrets),
                  self.redact(error, extra_secrets), code, request_id]
        await self._run(lambda db: db.execute(
            """UPDATE requests SET status=?,ttft_ms=?,duration_ms=?,content=?,reasoning_content=?,
               error=?,code=? WHERE id=?""", values,
        ).rowcount)

    @staticmethod
    def _row(row, detail=False):
        data = dict(row)
        if detail:
            data["messages"] = json.loads(data["messages"])
        return data

    async def listing(self, *, limit=20, offset=0, status=None, model=None, account_id=None):
        where, params = [], []
        for column, value in (("status", status), ("model", model), ("account_id", account_id)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = " WHERE " + " AND ".join(where) if where else ""
        def query(db):
            self._prune(db)
            count = db.execute("SELECT count(*) FROM requests" + clause, params).fetchone()[0]
            rows = db.execute(
                "SELECT id,started_at,account_id,model,status,ttft_ms,duration_ms,error,code FROM requests"
                + clause + " ORDER BY started_at DESC LIMIT ? OFFSET ?", [*params, limit, offset],
            )
            return {"data": [self._row(r) for r in rows], "total": count}
        return await self._run(query)

    async def detail(self, request_id):
        def query(db):
            self._prune(db)
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise GatewayError(404, "log_not_found", "请求日志不存在或已删除。")
            return self._row(row, True)
        return await self._run(query)

    async def delete(self, ids: list[str] | None):
        def delete(db):
            if ids is None:
                return db.execute("DELETE FROM requests WHERE status != 'running'").rowcount
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            return db.execute(
                f"DELETE FROM requests WHERE status != 'running' AND id IN ({placeholders})", ids,
            ).rowcount
        return await self._run(delete)

    async def summary(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        def query(db):
            self._prune(db)
            row = db.execute("""SELECT count(*) AS total,
                coalesce(sum(status='success'),0) AS success,
                coalesce(sum(status IN ('failed','cancelled','interrupted')),0) AS failed,
                avg(ttft_ms) AS average_ttft_ms, avg(duration_ms) AS average_duration_ms
                FROM requests WHERE started_at >= ?""", (cutoff,)).fetchone()
            result = dict(row)
            result["hourly"] = [dict(r) for r in db.execute(
                "SELECT substr(started_at,1,13) AS hour,count(*) AS count FROM requests "
                "WHERE started_at >= ? GROUP BY hour ORDER BY hour", (cutoff,),
            )]
            return result
        return await self._run(query)
