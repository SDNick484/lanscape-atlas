-- "Not mine": Bluetooth candidates the owner hid. Only a salted HMAC of the identity key
-- is stored (never the address or iBeacon id), with a label for the Status page.
CREATE TABLE ble_salt (
    id   INTEGER PRIMARY KEY CHECK (id = 1),
    salt BLOB NOT NULL
);
INSERT INTO ble_salt VALUES (1, randomblob(32));

CREATE TABLE ble_hidden (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    digest    TEXT NOT NULL UNIQUE,
    label     TEXT NOT NULL,
    hidden_at TEXT NOT NULL
);
