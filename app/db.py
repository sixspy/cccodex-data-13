import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # 并发建附页时让后到的写事务等待写锁，由 BEGIN IMMEDIATE 决定唯一赢家
        g.db.execute(
            f"PRAGMA busy_timeout = {current_app.config.get('SQLITE_BUSY_TIMEOUT_MS', 2000)}"
        )
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, item_key)
            );

            -- 目录声明：每个编码应附份数与附页模板名
            CREATE TABLE IF NOT EXISTS catalog_declarations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                template_name TEXT NOT NULL,
                required_copies INTEGER NOT NULL CHECK (required_copies >= 1),
                UNIQUE(job_id, code)
            );

            -- 账册行：应附 / 可不附
            CREATE TABLE IF NOT EXISTS ledger_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                required INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 附页库：附页是独立的库页（不是行内备注）
            CREATE TABLE IF NOT EXISTS attachment_pages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                page_name TEXT NOT NULL,
                seq INTEGER NOT NULL,
                operator TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(job_id, page_name, seq)
            );

            -- 当日核齐口径锁定（只能选一种，当日不能改口）
            CREATE TABLE IF NOT EXISTS reconcile_policy_locks (
                lock_date TEXT PRIMARY KEY,
                policy TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            -- 核齐会话：打开核齐瞬间产生，携带条数快照
            CREATE TABLE IF NOT EXISTS reconcile_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                policy TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                snapshot_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                opened_at TEXT NOT NULL DEFAULT (datetime('now')),
                closed_at TEXT
            );
            """
        )
        db.commit()
