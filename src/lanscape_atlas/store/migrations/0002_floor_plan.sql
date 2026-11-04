CREATE TABLE floor_plan (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    width      INTEGER NOT NULL CHECK (width > 0),
    height     INTEGER NOT NULL CHECK (height > 0),
    sha256     TEXT NOT NULL,
    file_name  TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
ALTER TABLE device ADD COLUMN roaming INTEGER NOT NULL DEFAULT 0 CHECK (roaming IN (0, 1));
