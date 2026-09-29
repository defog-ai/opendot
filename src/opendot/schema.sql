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
