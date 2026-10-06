-- AI Companion 记忆层数据库结构
--
-- 设计原则：
-- 1. 原始消息永不删除（messages），派生记忆（摘要/事件/画像）可重建。
-- 2. FTS5 使用 trigram 分词器：Unicode61 对中文按空白切分，中文检索会全部落空；
--    trigram 支持 ≥3 字符的子串匹配（实测确认 2 字符无结果）。
--    因此 2 字符查询由 storage 层回退到 LIKE。
-- 3. 所有表按 umo（unified_msg_origin）隔离，跨平台互不干扰。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- 短期会话记忆：原始消息
-- ============================================================
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    umo           TEXT    NOT NULL,
    conversation  TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL,              -- user | assistant | event
    sender_id     TEXT    NOT NULL DEFAULT '',
    sender_name   TEXT    NOT NULL DEFAULT '',
    content       TEXT    NOT NULL,              -- 已渲染的可读文本
    raw           TEXT    NOT NULL DEFAULT '{}', -- 原始消息链 JSON
    created_at    REAL    NOT NULL,
    is_proactive  INTEGER NOT NULL DEFAULT 0     -- 是否为 bot 主动发起
);
CREATE INDEX IF NOT EXISTS idx_messages_umo_time ON messages(umo, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sender   ON messages(umo, sender_id, created_at DESC);

-- 中文全文检索（trigram 子串匹配，≥3 字符）
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id',
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

-- ============================================================
-- 会话运行态持久化（供主动消息跨重启恢复）
-- ============================================================
CREATE TABLE IF NOT EXISTS session_state (
    umo                TEXT PRIMARY KEY,
    last_message_ts    REAL NOT NULL DEFAULT 0,
    unanswered_count   INTEGER NOT NULL DEFAULT 0,
    last_proactive_ts  REAL NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- 人物画像（长期记忆：每个人一份）
-- ============================================================
CREATE TABLE IF NOT EXISTS profiles (
    entity_id     TEXT    NOT NULL,
    umo_scope     TEXT    NOT NULL DEFAULT '',   -- '' = 全局（跨会话可见）
    display_name  TEXT    NOT NULL DEFAULT '',
    traits        TEXT    NOT NULL DEFAULT '[]', -- JSON: ["开朗", "话痨"]
    style         TEXT    NOT NULL DEFAULT '',   -- 说话风格观察
    affinity      REAL    NOT NULL DEFAULT 0.0,  -- 好感度（预留给后续版本）
    first_seen    REAL    NOT NULL DEFAULT 0,
    last_seen     REAL    NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (entity_id, umo_scope)
);

-- 别名归一：昵称 / @ / 平台 ID -> 稳定 entity_id
CREATE TABLE IF NOT EXISTS entity_aliases (
    umo_scope  TEXT NOT NULL DEFAULT '',
    alias      TEXT NOT NULL,
    entity_id  TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (umo_scope, alias)
);

-- ============================================================
-- 人与人之间的关系（预留给后续版本：姐妹 / 恋人 / 同事 …）
-- ============================================================
CREATE TABLE IF NOT EXISTS relations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    umo_scope          TEXT NOT NULL DEFAULT '',
    subject_id         TEXT NOT NULL,
    predicate          TEXT NOT NULL,            -- 关系名，如「姐妹」「恋人」
    object_id          TEXT NOT NULL,
    strength           REAL NOT NULL DEFAULT 0.5,
    evidence_event_ids TEXT NOT NULL DEFAULT '[]',
    last_reinforced    REAL NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(umo_scope, subject_id);

-- ============================================================
-- 事件记忆（预留给后续版本：把发生的事总结成事件线）
-- ============================================================
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    umo_scope     TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL,
    summary       TEXT    NOT NULL DEFAULT '',
    event_type    TEXT    NOT NULL DEFAULT '',
    importance    REAL    NOT NULL DEFAULT 0.5,
    occurred_at   REAL    NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS event_participants (
    event_id   INTEGER NOT NULL,
    entity_id  TEXT    NOT NULL,
    role       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, entity_id)
);
