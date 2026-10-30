-- lanscape-atlas schema, version 1. Timestamps are UTC ISO-8601 strings.

CREATE TABLE place (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE,
    floor   TEXT,
    polygon TEXT
);

CREATE TABLE device (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT,
    type        TEXT,
    icon        TEXT,
    place_id    INTEGER REFERENCES place(id) ON DELETE SET NULL,
    pos_x       REAL,
    pos_y       REAL,
    owner       TEXT,
    notes       TEXT,
    trust       TEXT NOT NULL DEFAULT 'new' CHECK (trust IN ('known','new','unwanted')),
    scan_policy TEXT NOT NULL DEFAULT 'normal' CHECK (scan_policy IN ('normal','never')),
    online      INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE interface (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE RESTRICT,
    kind       TEXT NOT NULL CHECK (kind IN ('ethernet','wifi','bluetooth','zwave','unknown')),
    address    TEXT NOT NULL,
    is_random  INTEGER NOT NULL DEFAULT 0,
    vendor     TEXT,
    ap_node    TEXT,
    band       TEXT,
    hints      TEXT NOT NULL DEFAULT '{}',
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE (kind, address)
);
CREATE INDEX idx_interface_device ON interface(device_id);

CREATE TABLE ip_lease (
    id           INTEGER PRIMARY KEY,
    interface_id INTEGER NOT NULL REFERENCES interface(id) ON DELETE CASCADE,
    ipv4         TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    closed       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_ip_lease_ipv4 ON ip_lease(ipv4, last_seen);
CREATE INDEX idx_ip_lease_interface ON ip_lease(interface_id);

CREATE TABLE service (
    id         INTEGER PRIMARY KEY,
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    port       INTEGER NOT NULL,
    proto      TEXT NOT NULL,
    name       TEXT,
    product    TEXT,
    version    TEXT,
    source     TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    UNIQUE (device_id, port, proto)
);

CREATE TABLE link (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    a_device_id INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    b_device_id INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('wired','wifi','mesh_backhaul')),
    detail      TEXT,
    source      TEXT NOT NULL CHECK (source IN ('manual','discovered')),
    layout      TEXT
);
CREATE INDEX idx_link_a ON link(a_device_id);
CREATE INDEX idx_link_b ON link(b_device_id);

CREATE TABLE observation (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    seen_at      TEXT NOT NULL,
    payload      TEXT NOT NULL,
    interface_id INTEGER REFERENCES interface(id) ON DELETE SET NULL,
    attached     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_observation_seen_at ON observation(seen_at);
-- Re-ingesting the same observation stores nothing new.
CREATE UNIQUE INDEX idx_observation_dedup ON observation(source, seen_at, payload);
CREATE INDEX idx_observation_interface ON observation(interface_id);

CREATE TABLE field_source (
    entity    TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    field     TEXT NOT NULL,
    actor     TEXT NOT NULL,
    set_at    TEXT NOT NULL,
    PRIMARY KEY (entity, entity_id, field)
);

CREATE TABLE batch (
    id              INTEGER PRIMARY KEY,
    actor           TEXT NOT NULL,
    at              TEXT NOT NULL,
    undoes_batch_id INTEGER REFERENCES batch(id)
);

CREATE TABLE change (
    id         INTEGER PRIMARY KEY,
    batch_id   INTEGER NOT NULL REFERENCES batch(id),
    entity     TEXT NOT NULL,
    entity_id  INTEGER NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    old_actor  TEXT,
    old_set_at TEXT
);
CREATE INDEX idx_change_batch ON change(batch_id);
CREATE INDEX idx_change_entity ON change(entity, entity_id, field);

CREATE TABLE topology_layout (
    device_id INTEGER PRIMARY KEY REFERENCES device(id) ON DELETE CASCADE,
    x         REAL,
    y         REAL
);

CREATE TABLE collector_status (
    name                 TEXT PRIMARY KEY,
    last_ok              TEXT,
    last_error           TEXT,
    last_error_at        TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_run_at          TEXT
);
