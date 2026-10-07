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
-- 人物与关系（长期记忆：全局，跨会话同一人）
--
-- 人的记忆是「对每个人都有一份印象，并且知道这些人彼此是什么关系」。
-- 因此 entity_id 使用平台用户 ID（全局稳定），不做会话隔离：
-- 同一个人在不同群里仍是同一个人。
-- ============================================================

-- 实体（人）
CREATE TABLE IF NOT EXISTS entities (
    entity_id         TEXT PRIMARY KEY,
    last_name         TEXT NOT NULL DEFAULT '',
    first_seen        REAL NOT NULL DEFAULT 0,
    last_seen         REAL NOT NULL DEFAULT 0,
    interaction_count INTEGER NOT NULL DEFAULT 0
);

-- 别名归一：昵称 / 群名片 / @ 显示名 -> entity_id
CREATE TABLE IF NOT EXISTS entity_aliases (
    alias      TEXT PRIMARY KEY,
    entity_id  TEXT NOT NULL,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_aliases_entity ON entity_aliases(entity_id);

-- 人物画像：对每个人的一份印象
CREATE TABLE IF NOT EXISTS profiles (
    entity_id   TEXT PRIMARY KEY,
    traits      TEXT NOT NULL DEFAULT '[]',
    style       TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    affinity    REAL NOT NULL DEFAULT 0.0,
    updated_at  REAL NOT NULL DEFAULT 0
);

-- 关系图谱：A 与 B 是什么关系（姐妹 / 恋人 / 同事 …）
CREATE TABLE IF NOT EXISTS relations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id         TEXT NOT NULL,
    predicate          TEXT NOT NULL,
    object_id          TEXT NOT NULL,
    strength           REAL NOT NULL DEFAULT 0.5,
    evidence           TEXT NOT NULL DEFAULT '[]',
    last_reinforced    REAL NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL DEFAULT 0,
    UNIQUE(subject_id, predicate, object_id)
);
CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id);
CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id);

-- 关系抽取进度（避免重复总结同一批消息）
CREATE TABLE IF NOT EXISTS extraction_state (
    umo              TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL DEFAULT 0,
    updated_at       REAL NOT NULL DEFAULT 0
);

-- ============================================================
-- 事件记忆：把发生的关键事情总结成事件线
-- 事件属于某个会话（umo_scope），参与者指向全局实体。
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
CREATE INDEX IF NOT EXISTS idx_events_scope_time
    ON events(umo_scope, occurred_at DESC);
CREATE TABLE IF NOT EXISTS event_participants (
    event_id   INTEGER NOT NULL,
    entity_id  TEXT    NOT NULL,
    role       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (event_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_event_participants_entity
    ON event_participants(entity_id);

-- ============================================================
-- 群画像：对一个群的长期认知（聊什么、氛围如何、都有谁）
--
-- 像真人一样，对不熟的群先观望、慢慢形成印象。这份画像随新消息
-- 滚动更新，供读空气判断「此刻该不该搭话、怎么搭」。
-- ============================================================
CREATE TABLE IF NOT EXISTS group_profiles (
    umo                TEXT PRIMARY KEY,
    profile            TEXT NOT NULL DEFAULT '',
    message_count      INTEGER NOT NULL DEFAULT 0,
    familiar           INTEGER NOT NULL DEFAULT 0,   -- 熟悉度 0~100
    updated_at         REAL NOT NULL DEFAULT 0
);
