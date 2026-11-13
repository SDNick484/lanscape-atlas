-- Schema v6 (spec §6.1): shapes drawn on the floor plan. Picture only: rooms stay the
-- place.polygon outlines that placement and Bluetooth use. No foreign keys: shapes
-- reference neither rooms nor each other.
--   kind: all four kinds are allowed here; the Service validates the ones it supports.
--   geometry: JSON by kind, normalized [0, 1] coordinates (see Service.save_shapes).
--   label: furniture name or label text (NULL for walls and doors).
-- AUTOINCREMENT so a deleted id is never reused: undo re-inserts a shape under its id.
CREATE TABLE plan_shape (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL CHECK (kind IN ('wall', 'door', 'furniture', 'label')),
    geometry   TEXT NOT NULL,
    label      TEXT,
    created_at TEXT NOT NULL
);
