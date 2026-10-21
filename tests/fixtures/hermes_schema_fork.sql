-- Hermes Agent Kanban board schema as written by a fork that adds per-run model
-- tracking and concurrency caps (read from a live board with `sqlite3 .schema`).
-- Comments stripped. Fork-only versus upstream: task_runs.model_used,
-- provider_used, fallback_from; tasks.block_reason, created_by_task_id,
-- created_by_run_id; the dispatcher_state table; three block_reason triggers.
-- The fork also writes task_events of kind 'capped' / 'capped_alerted' and
-- 'model_fallback' (upstream never does). It lacks upstream's newer
-- tasks.worker_started_at and task_runs.worker_started_at.
CREATE TABLE tasks (
    id                   TEXT PRIMARY KEY,
    title                TEXT NOT NULL,
    body                 TEXT,
    assignee             TEXT,
    status               TEXT NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           TEXT,
    created_at           INTEGER NOT NULL,
    started_at           INTEGER,
    completed_at         INTEGER,
    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',
    workspace_path       TEXT,
    branch_name          TEXT,
    project_id           TEXT,
    claim_lock           TEXT,
    claim_expires        INTEGER,
    tenant               TEXT,
    result               TEXT,
    idempotency_key      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   TEXT,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    INTEGER,
    current_run_id       INTEGER,
    workflow_template_id TEXT,
    current_step_key     TEXT,
    skills               TEXT,
    model_override       TEXT,
    max_retries          INTEGER,
    goal_mode            INTEGER NOT NULL DEFAULT 0,
    goal_max_turns       INTEGER,
    session_id           TEXT,
    block_kind           TEXT,
    block_recurrences    INTEGER NOT NULL DEFAULT 0
, provider_override TEXT, reasoning_effort TEXT, block_reason TEXT, created_by_task_id TEXT, created_by_run_id INTEGER, completion_contract TEXT);
CREATE TABLE task_links (
    parent_id  TEXT NOT NULL,
    child_id   TEXT NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE task_comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    run_id     INTEGER,
    kind       TEXT NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE task_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             TEXT NOT NULL,
    profile             TEXT,
    step_key            TEXT,
    status              TEXT NOT NULL,
    claim_lock          TEXT,
    claim_expires       INTEGER,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   INTEGER,
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER,
    outcome             TEXT,
    summary             TEXT,
    metadata            TEXT,
    error               TEXT
, model_used TEXT, provider_used TEXT, fallback_from TEXT);
CREATE TABLE task_attachments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL,
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    content_type TEXT,
    size         INTEGER NOT NULL DEFAULT 0,
    uploaded_by  TEXT,
    created_at   INTEGER NOT NULL
);
CREATE TABLE kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    notifier_profile TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0, chat_type TEXT, delivery_metadata TEXT, delivery_mode TEXT NOT NULL DEFAULT 'notify', user_id_alt TEXT, last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
CREATE INDEX idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX idx_tasks_status          ON tasks(status);
CREATE INDEX idx_links_child           ON task_links(child_id);
CREATE INDEX idx_links_parent          ON task_links(parent_id);
CREATE INDEX idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX idx_runs_status           ON task_runs(status);
CREATE INDEX idx_attachments_task      ON task_attachments(task_id, created_at);
CREATE INDEX idx_notify_task           ON kanban_notify_subs(task_id);
CREATE INDEX idx_tasks_tenant ON tasks(tenant);
CREATE INDEX idx_tasks_idempotency ON tasks(idempotency_key);
CREATE INDEX idx_tasks_session_id ON tasks(session_id);
CREATE INDEX idx_events_run ON task_events(run_id, id);
CREATE TRIGGER trg_tasks_block_reason_insert
            BEFORE INSERT ON tasks
            WHEN NEW.status = 'blocked'
             AND TRIM(COALESCE(NEW.block_reason, '')) = ''
            BEGIN
                SELECT RAISE(ABORT, 'block reason is required');
            END;
CREATE TRIGGER trg_tasks_block_reason_update
            BEFORE UPDATE ON tasks
            WHEN NEW.status = 'blocked'
             AND TRIM(COALESCE(NEW.block_reason, '')) = ''
            BEGIN
                SELECT RAISE(ABORT, 'block reason is required');
            END;
CREATE TRIGGER trg_tasks_block_reason_clear
            AFTER UPDATE OF status ON tasks
            WHEN OLD.status = 'blocked'
             AND NEW.status != 'blocked'
             AND NEW.block_reason IS NOT NULL
            BEGIN
                UPDATE tasks SET block_reason = NULL WHERE id = NEW.id;
            END;
CREATE TABLE dispatcher_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
