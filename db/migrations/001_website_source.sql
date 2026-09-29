-- Website the generator reads before writing (per campaign), and the page a
-- draft was based on. Idempotent: scripts/bootstrap.sh applies every file in
-- db/migrations on each run.
ALTER TABLE campaigns     ADD COLUMN IF NOT EXISTS source_url  TEXT;
ALTER TABLE content_queue ADD COLUMN IF NOT EXISTS source_link TEXT;
