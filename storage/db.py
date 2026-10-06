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

# 数据库结构版本：v0.4 起为 2（人物/关系表改为全局实体图）
SCHEMA_VERSION = 2


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
        self._migrate(conn)
        schema = self.schema_path.read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()
        self._conn = conn
        logger.info(f"[ai_companion] 记忆数据库就绪: {self.db_path}")

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """结构升级。

        v0.1~v0.3 的人物/关系表是占位骨架（从未写入过数据），v0.4 起改为
        全局实体图结构。旧表若存在且为空，直接重建；有数据则保留不动。
        """
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version >= SCHEMA_VERSION:
            return

        legacy = ("profiles", "entity_aliases", "relations")
        for table in legacy:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            cols = {
                r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            # 旧结构带有 umo_scope 列；新结构没有
            if "umo_scope" in cols:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                if count == 0:
                    conn.execute(f"DROP TABLE {table}")
                else:
                    logger.warning(
                        f"[ai_companion] {table} 含旧结构数据，保留不迁移（{count} 行）"
                    )
        conn.commit()

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
    # 实体（人）与别名
    # ------------------------------------------------------------------
    async def touch_entity(self, entity_id: str, display_name: str = "") -> None:
        """记录一次「见到这个人」。"""
        if not entity_id:
            return
        ts = time.time()

        def _op() -> None:
            self._execute(
                "INSERT INTO entities "
                "(entity_id, last_name, first_seen, last_seen, interaction_count) "
                "VALUES (?, ?, ?, ?, 1) "
                "ON CONFLICT(entity_id) DO UPDATE SET "
                "last_name = CASE WHEN excluded.last_name != '' "
                "THEN excluded.last_name ELSE entities.last_name END, "
                "last_seen = excluded.last_seen, "
                "interaction_count = entities.interaction_count + 1",
                (entity_id, display_name, ts, ts),
                commit=True,
            )

        await self._run(_op)

    async def bind_alias(self, alias: str, entity_id: str) -> None:
        """把一个称呼绑定到实体，供归一使用。"""
        alias = (alias or "").strip()
        if not alias or not entity_id:
            return

        def _op() -> None:
            self._execute(
                "INSERT INTO entity_aliases (alias, entity_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(alias) DO UPDATE SET "
                "entity_id = excluded.entity_id, updated_at = excluded.updated_at",
                (alias, entity_id, time.time()),
                commit=True,
            )

        await self._run(_op)

    async def resolve_alias(self, alias: str) -> str | None:
        """按称呼反查实体 ID。"""
        alias = (alias or "").strip()
        if not alias:
            return None

        def _op():
            row = self._execute(
                "SELECT entity_id FROM entity_aliases WHERE alias = ?",
                (alias,),
                fetch="one",
            )
            return row["entity_id"] if row else None

        return await self._run(_op)

    async def get_entity(self, entity_id: str) -> dict | None:
        def _op():
            row = self._execute(
                "SELECT * FROM entities WHERE entity_id = ?", (entity_id,), fetch="one"
            )
            return dict(row) if row else None

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 人物画像
    # ------------------------------------------------------------------
    async def upsert_profile(
        self,
        entity_id: str,
        *,
        traits: list[str] | None = None,
        style: str | None = None,
        notes: str | None = None,
        affinity: float | None = None,
    ) -> None:
        """写入/更新对某人的印象（仅覆盖显式传入的字段）。"""

        def _op() -> None:
            existing = self._execute(
                "SELECT * FROM profiles WHERE entity_id = ?", (entity_id,), fetch="one"
            )
            if existing is None:
                self._execute(
                    "INSERT INTO profiles "
                    "(entity_id, traits, style, notes, affinity, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        entity_id,
                        json.dumps(traits or [], ensure_ascii=False),
                        style or "",
                        notes or "",
                        float(affinity if affinity is not None else 0.0),
                        time.time(),
                    ),
                    commit=True,
                )
                return

            old_traits = self._load_json_list(existing["traits"])
            merged_traits = old_traits
            if traits:
                merged_traits = list(dict.fromkeys([*old_traits, *traits]))[:20]

            self._execute(
                "UPDATE profiles SET traits = ?, style = ?, notes = ?, "
                "affinity = ?, updated_at = ? WHERE entity_id = ?",
                (
                    json.dumps(merged_traits, ensure_ascii=False),
                    style if style else existing["style"],
                    notes if notes else existing["notes"],
                    float(affinity)
                    if affinity is not None
                    else float(existing["affinity"]),
                    time.time(),
                    entity_id,
                ),
                commit=True,
            )

        await self._run(_op)

    async def get_profile(self, entity_id: str) -> dict | None:
        def _op():
            row = self._execute(
                "SELECT * FROM profiles WHERE entity_id = ?", (entity_id,), fetch="one"
            )
            if not row:
                return None
            data = dict(row)
            data["traits"] = self._load_json_list(data.get("traits"))
            return data

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 关系图谱
    # ------------------------------------------------------------------
    async def upsert_relation(
        self,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        strength: float = 0.5,
        evidence: str = "",
    ) -> None:
        """写入/强化一条关系。同一 (主语, 关系, 宾语) 只会存在一条，重复出现即强化。"""
        if not (subject_id and predicate and object_id):
            return
        ts = time.time()

        def _op() -> None:
            existing = self._execute(
                "SELECT * FROM relations WHERE subject_id = ? AND predicate = ? "
                "AND object_id = ?",
                (subject_id, predicate, object_id),
                fetch="one",
            )
            if existing is None:
                self._execute(
                    "INSERT INTO relations "
                    "(subject_id, predicate, object_id, strength, evidence, "
                    " last_reinforced, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        subject_id,
                        predicate,
                        object_id,
                        max(0.0, min(1.0, strength)),
                        json.dumps([evidence] if evidence else [], ensure_ascii=False),
                        ts,
                        ts,
                    ),
                    commit=True,
                )
                return

            # 再次观察到 -> 关系变强，并追加证据
            new_strength = min(1.0, max(float(existing["strength"]), 0.0) + 0.15)
            ev = self._load_json_list(existing["evidence"])
            if evidence and evidence not in ev:
                ev.append(evidence)
            self._execute(
                "UPDATE relations SET strength = ?, evidence = ?, last_reinforced = ? "
                "WHERE id = ?",
                (
                    new_strength,
                    json.dumps(ev[-10:], ensure_ascii=False),
                    ts,
                    existing["id"],
                ),
                commit=True,
            )

        await self._run(_op)

    async def get_relations(
        self, entity_id: str, *, limit: int = 20
    ) -> list[dict]:
        """取出与某人相关的全部关系（无论他/她是主语还是宾语）。"""

        def _op():
            rows = self._execute(
                "SELECT * FROM relations WHERE subject_id = ? OR object_id = ? "
                "ORDER BY strength DESC, last_reinforced DESC LIMIT ?",
                (entity_id, entity_id, limit),
                fetch="all",
            )
            out = []
            for r in rows or []:
                d = dict(r)
                d["evidence"] = self._load_json_list(d.get("evidence"))
                out.append(d)
            return out

        return await self._run(_op)

    async def get_active_relations(self, *, limit: int = 200) -> list[dict]:
        def _op():
            rows = self._execute(
                "SELECT * FROM relations ORDER BY strength DESC LIMIT ?",
                (limit,),
                fetch="all",
            )
            return [dict(r) for r in (rows or [])]

        return await self._run(_op)

    # ------------------------------------------------------------------
    # 关系抽取进度
    # ------------------------------------------------------------------
    async def get_extraction_cursor(self, umo: str) -> int:
        def _op():
            row = self._execute(
                "SELECT last_message_id FROM extraction_state WHERE umo = ?",
                (umo,),
                fetch="one",
            )
            return int(row["last_message_id"]) if row else 0

        return await self._run(_op)

    async def set_extraction_cursor(self, umo: str, last_message_id: int) -> None:
        def _op() -> None:
            self._execute(
                "INSERT INTO extraction_state (umo, last_message_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "last_message_id = excluded.last_message_id, "
                "updated_at = excluded.updated_at",
                (umo, last_message_id, time.time()),
                commit=True,
            )

        await self._run(_op)

    async def messages_since(
        self, umo: str, last_id: int, *, limit: int = 50
    ) -> list[sqlite3.Row]:
        """取出某会话在主键之后的消息（含 id，供推进游标）。"""

        def _op():
            rows = self._execute(
                "SELECT * FROM messages WHERE umo = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (umo, last_id, max(1, limit)),
                fetch="all",
            )
            return list(rows or [])

        return await self._run(_op)

    @staticmethod
    def _load_json_list(raw: Any) -> list:
        if not raw:
            return []
        if isinstance(raw, list):
            return raw
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

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

    # ------------------------------------------------------------------
    # 会话运行态（主动消息用，跨重启恢复）
    # ------------------------------------------------------------------
    async def upsert_session_state(
        self,
        umo: str,
        *,
        last_message_ts: float,
        unanswered_count: int,
        last_proactive_ts: float,
    ) -> None:
        def _op() -> None:
            self._execute(
                "INSERT INTO session_state "
                "(umo, last_message_ts, unanswered_count, last_proactive_ts, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(umo) DO UPDATE SET "
                "last_message_ts = excluded.last_message_ts, "
                "unanswered_count = excluded.unanswered_count, "
                "last_proactive_ts = excluded.last_proactive_ts, "
                "updated_at = excluded.updated_at",
                (
                    umo,
                    last_message_ts,
                    unanswered_count,
                    last_proactive_ts,
                    time.time(),
                ),
                commit=True,
            )

        await self._run(_op)

    async def load_session_states(self) -> list[sqlite3.Row]:
        def _op():
            rows = self._execute("SELECT * FROM session_state", fetch="all")
            return list(rows or [])

        return await self._run(_op)

    async def vacuum(self) -> None:
        await self._run(lambda: self._execute("VACUUM", commit=True))
