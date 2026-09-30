-- OpenDot state, SQLite. Every timestamp is UTC text written by models.to_iso
-- ("YYYY-MM-DDTHH:MM:SS.ffffffZ"), so text comparison matches time order.
-- JSON columns hold text produced by json.dumps.

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per unit of work. Leases let a worker hold a task while it runs a step.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'running', 'awaiting_approval', 'awaiting_reply', 'waiting',
        'done', 'failed', 'stopped', 'skipped')),
    source TEXT NOT NULL CHECK (source IN ('cli', 'channel', 'schedule')),
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    thread TEXT,
    requester TEXT NOT NULL,
    profile TEXT NOT NULL,
    text TEXT NOT NULL,
    schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
    parent_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    backend_thread_id TEXT,
    wait_until TEXT,
    summary TEXT,
    last_error TEXT,
    turns_used INTEGER NOT NULL DEFAULT 0,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    active_seconds REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_claim_idx ON tasks (state, created_at);
CREATE INDEX IF NOT EXISTS tasks_thread_idx ON tasks (channel, conversation, thread);
CREATE INDEX IF NOT EXISTS tasks_wait_idx ON tasks (state, wait_until);

-- One row per model step run for a task (work, review, reflect).
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step TEXT NOT NULL CHECK (step IN ('work', 'review', 'reflect')),
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'running', 'succeeded', 'failed', 'schema_error', 'timed_out', 'interrupted')),
    backend TEXT NOT NULL,
    backend_thread_id TEXT,
    run_path TEXT,
    output TEXT,
    usage TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (task_id, attempt_number)
);

-- Every message a channel delivered. The UNIQUE constraint stops duplicate intake.
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    external_id TEXT NOT NULL,
    conversation TEXT NOT NULL,
    thread TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'request', 'clarification', 'steer', 'stop', 'follow_up', 'command', 'ignored')),
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    received_at TEXT NOT NULL,
    applied_at TEXT,
    UNIQUE (channel, external_id)
);
CREATE INDEX IF NOT EXISTS messages_task_idx ON messages (task_id, applied_at);

-- Polling position per channel and conversation.
CREATE TABLE IF NOT EXISTS cursors (
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, conversation)
);

-- Threads the requester paused; intake ignores new work there until resumed.
CREATE TABLE IF NOT EXISTS thread_pauses (
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    thread TEXT NOT NULL,
    paused_by TEXT NOT NULL,
    paused_at TEXT NOT NULL,
    PRIMARY KEY (channel, conversation, thread)
);

-- Messages waiting to be delivered, with retry bookkeeping.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    thread TEXT,
    text TEXT NOT NULL,
    reaction TEXT,
    reply_to TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    external_id TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox (channel, id) WHERE delivered_at IS NULL;

-- Actions the agent proposed, as the host prepared them.
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    level TEXT NOT NULL CHECK (level IN ('allow', 'preapproved', 'ask', 'hand_off')),
    status TEXT NOT NULL CHECK (status IN (
        'proposed', 'refused', 'denied', 'awaiting_approval', 'approved',
        'executed', 'failed', 'handed_off')),
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS actions_task_idx ON actions (task_id);

-- Approvals are tied to one task. A single-use approval also carries the digest
-- of the exact payload it covers; an until-task-end approval covers kind + target.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    target TEXT NOT NULL,
    payload_digest TEXT,
    mode TEXT NOT NULL CHECK (mode IN ('single_use', 'until_task_end')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'granted', 'denied', 'used', 'expired')),
    requested_at TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT,
    expires_at TEXT,
    used_at TEXT,
    CHECK (mode = 'until_task_end' OR payload_digest IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS approvals_task_idx ON approvals (task_id, kind, target);

-- Permission rules. Agent-drafted rules stay pending until the operator approves.
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '*',
    level TEXT NOT NULL CHECK (level IN ('allow', 'preapproved', 'ask', 'hand_off')),
    status TEXT NOT NULL CHECK (status IN ('active', 'pending')),
    source TEXT NOT NULL CHECK (source IN ('operator', 'config', 'agent')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    approved_at TEXT
);

-- Saved schedules. Each run starts a new task owned by creator, with no approvals
-- carried over; results go only to the stored destination.
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    what TEXT NOT NULL,
    cadence TEXT NOT NULL,
    tz TEXT NOT NULL,
    until TEXT,
    notify_rule TEXT NOT NULL CHECK (notify_rule IN ('always', 'changed')),
    channel TEXT NOT NULL,
    conversation TEXT NOT NULL,
    thread TEXT,
    creator TEXT NOT NULL,
    profile TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'ended')),
    next_run_at TEXT,
    last_run_at TEXT,
    last_result_hash TEXT,
    last_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS schedules_due_idx ON schedules (status, next_run_at);

-- Private notes, kept per requester profile.
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('requester', 'agent', 'operator')),
    source_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notes_profile_idx ON notes (profile, id);

-- Every reviewer verdict. Denial limits are computed from this log.
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'deny', 'escalate_to_user')),
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reviews_task_idx ON reviews (task_id, id);

-- Audit log: ignored approvals, refused actions, operator commands and similar.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_task_idx ON events (task_id, id);

-- ---------------------------------------------------------------------------
-- Schema version 2. New tables only: CREATE TABLE IF NOT EXISTS cannot add
-- columns to a table a version 1 database already has, so v0.2 adds none.
-- ---------------------------------------------------------------------------

-- Host state for each configured repository and its control clone.
CREATE TABLE IF NOT EXISTS repositories (
    name TEXT PRIMARY KEY,
    remote TEXT NOT NULL,
    control_path TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'unknown'
        CHECK (visibility IN ('unknown', 'public', 'private')),
    visibility_checked_at TEXT,
    last_fetched_at TEXT,
    updated_at TEXT NOT NULL
);

-- A task's own copy of a repository. The work step gets it writable, with .git
-- read-only; only the host commits and pushes from it.
CREATE TABLE IF NOT EXISTS worktrees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    base_ref TEXT NOT NULL,
    base_sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'removed')),
    created_at TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE (task_id, repository)
);

-- Branches, pull requests, issues and comments the host published. The marker
-- is hidden text in the body; a retry searches for it before creating anything.
CREATE TABLE IF NOT EXISTS github_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    action_id INTEGER REFERENCES actions(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('branch', 'pull_request', 'issue', 'issue_comment')),
    repository TEXT NOT NULL,
    marker TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('started', 'published', 'merged', 'closed', 'failed')),
    branch TEXT,
    head_sha TEXT,
    number INTEGER,
    url TEXT,
    external_id TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS github_publications_task_idx ON github_publications (task_id);
CREATE INDEX IF NOT EXISTS github_publications_open_idx
    ON github_publications (kind, state);

-- Every call a work step made through the MCP gateway, including refused ones.
CREATE TABLE IF NOT EXISTS gateway_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_token TEXT NOT NULL,
    attempt_id INTEGER REFERENCES attempts(id) ON DELETE SET NULL,
    server TEXT NOT NULL,
    tool TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('read', 'write')),
    arguments TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'refused')),
    result_bytes INTEGER NOT NULL DEFAULT 0,
    result_preview TEXT NOT NULL DEFAULT '',
    error TEXT,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS gateway_calls_task_idx ON gateway_calls (task_id, id);
CREATE INDEX IF NOT EXISTS gateway_calls_step_idx ON gateway_calls (step_token);

-- OAuth tokens for MCP servers. Only the host reads them; the database file is 0600.
CREATE TABLE IF NOT EXISTS connector_tokens (
    server TEXT PRIMARY KEY,
    token_type TEXT NOT NULL DEFAULT 'Bearer',
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at TEXT,
    scope TEXT NOT NULL DEFAULT '',
    client_info TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
