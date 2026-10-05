"""本地记忆数据库。

使用标准库 ``sqlite3``，因此不引入任何第三方运行时依赖。

关键约束：

* 连接只在一个专用线程（``asyncio.to_thread``）内创建与使用，规避
  ``sqlite3`` 默认的「同一连接不可跨线程」限制，同时避免阻塞事件循环。
* 写入统一走 ``call``（单写者），查询走只读连接池语义（简单起见仍复用同一连接，
  因 sqlite 的 ``check_same_thread=False`` 需要在受控线程内串行访问）。
* 全文检索使用 FTS5 ``trigram`` 分词器；2 字符查询会回退到 ``LIKE``。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from astrbot.api import logger

# trigram 分词器要求查询串至少 3 个字符才能命中，短查询回退 LIKE。
FTS_MIN_QUERY_CHARS = 3


class MemoryDB:
    """记忆层数据库门面。"""

    def __init__(self, db_path: Path, schema_path: Path) -> None:
        self.db_path = db_path
        self.schema_path = schema_path
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def connect(self) -> None:
        """建立连接并确保表结构存在。"""
        await self._run(self._connect_sync)

    def _connect_sync(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        schema = self.schema_path.read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.commit()
        self._conn = conn
        logger.info(f"[ai_companion] 记忆数据库就绪: {self.db_path}")

    async def close(self) -> None:
        async with self._lock:
            if self._conn is not None:
                conn, self._conn = self._conn, None
                await asyncio.to_thread(conn.close)

    @property
    def connected(self) -> bool:
        return self._conn is not None

    # ------------------------------------------------------------------
    # 执行辅助
    # ------------------------------------------------------------------
    async def _run(self, fn, *args):
        """在专用线程内串行执行数据库操作。"""
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def _execute(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        fetch: str | None = None,
        commit: bool = False,
    ):
        if self._conn is None:
            raise RuntimeError("MemoryDB 尚未连接")
        cur = self._conn.execute(sql, params)
        result = None
        if fetch == "all":
            result = cur.fetchall()
        elif fetch == "one":
            result = cur.fetchone()
        if commit:
            self._conn.commit()
        cur.close()
        return result

    # ------------------------------------------------------------------
    # 消息写入
    # ------------------------------------------------------------------
    async def insert_message(
        self,
        *,
        umo: str,
        role: str,
        content: str,
        sender_id: str = "",
        sender_name: str = "",
        conversation: str = "",
        raw: Any = None,
        is_proactive: bool = False,
        created_at: float | None = None,
    ) -> int:
        """写入一条消息，返回自增主键。"""
        payload = json.dumps(raw, ensure_ascii=False) if raw is not None else "{}"
        ts = time.time() if created_at is None else created_at

        def _op() -> int:
            cur = self._conn.execute(  # type: ignore[union-attr]
                "INSERT INTO messages "
                "(umo, conversation, role, sender_id, sender_name, content, raw, created_at, is_proactive) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    umo,
                    conversation,
                    role,
                    sender_id,
                    sender_name,
                    content,
                    payload,
                    ts,
                    1 if is_proactive else 0,
                ),
            )
            row_id = int(cur.lastrowid or 0)
            self._conn.commit()  # type: ignore[union-attr]
            cur.close()
            return row_id

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 短期记忆读取
    # ------------------------------------------------------------------
    async def recent_messages(
        self,
        umo: str,
        *,
        limit: int = 20,
        before: float | None = None,
    ) -> list[sqlite3.Row]:
        """读取某会话最近的消息（按时间正序返回，便于直接拼上下文）。"""

        def _op() -> list[sqlite3.Row]:
            if before is None:
                rows = self._execute(
                    "SELECT * FROM messages WHERE umo = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (umo, max(1, limit)),
                    fetch="all",
                )
            else:
                rows = self._execute(
                    "SELECT * FROM messages WHERE umo = ? AND created_at < ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ?",
                    (umo, before, max(1, limit)),
                    fetch="all",
                )
            return list(reversed(rows or []))

        return await self._run(_op)

    async def last_message_time(self, umo: str) -> float:
        """读取会话最后一条消息的时间戳（无记录返回 0）。"""

        def _op() -> float:
            row = self._execute(
                "SELECT created_at FROM messages WHERE umo = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (umo,),
                fetch="one",
            )
            return float(row["created_at"]) if row else 0.0

        return await self._run(_op)

    async def count_messages(self, umo: str | None = None) -> int:
        def _op() -> int:
            if umo is None:
                row = self._execute("SELECT COUNT(*) AS c FROM messages", fetch="one")
            else:
                row = self._execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE umo = ?",
                    (umo,),
                    fetch="one",
                )
            return int(row["c"]) if row else 0

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 历史检索
    # ------------------------------------------------------------------
    async def search_messages(
        self,
        query: str,
        *,
        umo: str | None = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        """全文检索历史消息。

        trigram 分词要求查询串 ≥3 字符；更短的查询（如两字中文词）无法命中
        FTS5，回退到 ``LIKE`` 扫描，保证「查旧记录」始终有结果。
        """
        query = (query or "").strip()
        if not query:
            return []

        def _op() -> list[sqlite3.Row]:
            if len(query) >= FTS_MIN_QUERY_CHARS:
                try:
                    if umo is None:
                        rows = self._execute(
                            "SELECT m.* FROM messages_fts f "
                            "JOIN messages m ON m.id = f.rowid "
                            "WHERE messages_fts MATCH ? "
                            "ORDER BY rank LIMIT ?",
                            (self._escape_fts(query), limit),
                            fetch="all",
                        )
                    else:
                        rows = self._execute(
                            "SELECT m.* FROM messages_fts f "
                            "JOIN messages m ON m.id = f.rowid "
                            "WHERE messages_fts MATCH ? AND m.umo = ? "
                            "ORDER BY rank LIMIT ?",
                            (self._escape_fts(query), umo, limit),
                            fetch="all",
                        )
                    if rows:
                        return list(rows)
                except sqlite3.OperationalError as e:
                    logger.warning(f"[ai_companion] FTS 检索失败，回退 LIKE: {e}")

            pattern = f"%{query}%"
            if umo is None:
                rows = self._execute(
                    "SELECT * FROM messages WHERE content LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (pattern, limit),
                    fetch="all",
                )
            else:
                rows = self._execute(
                    "SELECT * FROM messages WHERE content LIKE ? AND umo = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (pattern, umo, limit),
                    fetch="all",
                )
            return list(rows or [])

        return await self._run(_op)

    @staticmethod
    def _escape_fts(query: str) -> str:
        """把用户输入包装成 FTS5 字符串字面量，避免语法字符被解释为操作符。"""
        escaped = query.replace('"', '""')
        return f'"{escaped}"'

    # ------------------------------------------------------------------
    # 人物画像（P4 完整启用，此处先提供基础读写）
    # ------------------------------------------------------------------
    async def upsert_profile_seen(
        self,
        *,
        entity_id: str,
        umo_scope: str,
        display_name: str = "",
        when: float | None = None,
    ) -> None:
        ts = time.time() if when is None else when

        def _op() -> None:
            self._execute(
                "INSERT INTO profiles "
                "(entity_id, umo_scope, display_name, first_seen, last_seen, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_id, umo_scope) DO UPDATE SET "
                "display_name = CASE WHEN excluded.display_name != '' "
                "THEN excluded.display_name ELSE profiles.display_name END, "
                "last_seen = excluded.last_seen, updated_at = excluded.updated_at",
                (entity_id, umo_scope, display_name, ts, ts, ts),
                commit=True,
            )

        await self._run(_op)

    async def get_profile(self, entity_id: str, umo_scope: str = "") -> dict | None:
        def _op():
            row = self._execute(
                "SELECT * FROM profiles WHERE entity_id = ? AND umo_scope = ?",
                (entity_id, umo_scope),
                fetch="one",
            )
            return dict(row) if row else None

        return await self._run(_op)

    async def bind_alias(self, alias: str, entity_id: str, umo_scope: str = "") -> None:
        alias = (alias or "").strip()
        if not alias:
            return

        def _op() -> None:
            self._execute(
                "INSERT INTO entity_aliases (umo_scope, alias, entity_id, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(umo_scope, alias) DO UPDATE SET "
                "entity_id = excluded.entity_id, updated_at = excluded.updated_at",
                (umo_scope, alias, entity_id, time.time()),
                commit=True,
            )

        await self._run(_op)

    async def resolve_alias(self, alias: str, umo_scope: str = "") -> str | None:
        def _op():
            row = self._execute(
                "SELECT entity_id FROM entity_aliases WHERE umo_scope = ? AND alias = ?",
                (umo_scope, (alias or "").strip()),
                fetch="one",
            )
            return row["entity_id"] if row else None

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    async def distinct_sessions(self) -> list[str]:
        def _op():
            rows = self._execute(
                "SELECT DISTINCT umo FROM messages ORDER BY umo", fetch="all"
            )
            return [r["umo"] for r in (rows or [])]

        return await self._run(_op)

    async def vacuum(self) -> None:
        await self._run(lambda: self._execute("VACUUM", commit=True))
