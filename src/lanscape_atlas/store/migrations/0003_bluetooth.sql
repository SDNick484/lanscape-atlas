CREATE TABLE ble_identity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('address', 'ibeacon')),
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (kind, value)
);
CREATE INDEX ble_identity_device ON ble_identity(device_id);

CREATE TABLE ble_scanner (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL UNIQUE,
    name       TEXT,
    device_id  INTEGER REFERENCES device(id) ON DELETE SET NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE ble_sighting (
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    scanner_id INTEGER NOT NULL REFERENCES ble_scanner(id) ON DELETE CASCADE,
    minute     TEXT NOT NULL,
    place_id   INTEGER REFERENCES place(id) ON DELETE SET NULL,
    max_rssi   INTEGER NOT NULL,
    adverts    INTEGER NOT NULL,
    last_at    TEXT NOT NULL,
    PRIMARY KEY (device_id, scanner_id, minute)
) WITHOUT ROWID;
CREATE INDEX ble_sighting_last ON ble_sighting(device_id, last_at);
CREATE INDEX ble_sighting_minute ON ble_sighting(minute);
