-- Schema v5 (spec §5.1): blank plans, a real-world scale and display units.
-- SQLite cannot drop NOT NULL, so floor_plan is rebuilt (create, copy, drop, rename)
-- inside the migration transaction. Nothing references floor_plan by foreign key.
--   sha256/file_name: both NULL = a blank plan (width/height are millimetres and
--     width_mm = width); both set = an image plan (width/height are pixels).
--   width_mm: the plan's real-world width in mm; NULL = no scale (uncalibrated image).
--   units: display units only; everything is stored in mm or plan coordinates.
CREATE TABLE floor_plan_v5 (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    width      INTEGER NOT NULL CHECK (width > 0),
    height     INTEGER NOT NULL CHECK (height > 0),
    sha256     TEXT,
    file_name  TEXT,
    updated_at TEXT NOT NULL,
    width_mm   REAL CHECK (width_mm > 0),
    units      TEXT NOT NULL DEFAULT 'imperial' CHECK (units IN ('imperial', 'metric')),
    CHECK ((sha256 IS NULL) = (file_name IS NULL))
);
INSERT INTO floor_plan_v5(id, width, height, sha256, file_name, updated_at)
    SELECT id, width, height, sha256, file_name, updated_at FROM floor_plan;
DROP TABLE floor_plan;
ALTER TABLE floor_plan_v5 RENAME TO floor_plan;
