-- Schema v7: indexes for the hot lookups that were full scans.
--   interface(address, id): MAC lookup on every observation (ingestor._upsert_interface,
--     "WHERE address = ? ORDER BY id LIMIT 1"). UNIQUE(kind, address) cannot serve it
--     because address is its second column. id makes the ORDER BY a plain index read.
--   batch(undoes_batch_id): undo's "is this batch already undone" check and the
--     undone-batch listing (IS NOT NULL). Nothing else in batch is looked up by it.
--   device(place_id): placement's "devices in this room" query, and the device scan
--     behind place deletion (ON DELETE SET NULL).
-- Indexes only: no table or row changes. Planner statistics are refreshed by
-- PRAGMA optimize when a connection closes (store.db.open_db).
CREATE INDEX idx_interface_address ON interface(address, id);
CREATE INDEX idx_batch_undoes ON batch(undoes_batch_id);
CREATE INDEX idx_device_place ON device(place_id);
