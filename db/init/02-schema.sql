-- Social Automation Hub - CMS schema (database: social_hub)
-- Runs once on first boot of an empty Postgres volume. Safe to re-run manually.
-- Plain TEXT + CHECK constraints instead of Postgres ENUMs so NocoDB shows
-- them as editable single-selects and adding a status never needs a migration.

CREATE TABLE IF NOT EXISTS campaigns (
    id              SERIAL PRIMARY KEY,
    name            VARCHAR(200) NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    priority        INT NOT NULL DEFAULT 0,          -- highest active campaign wins the daily slot
    brand_name      VARCHAR(200),
    audience        TEXT,
    tone_guidelines TEXT,                            -- voice/tone rules fed to Claude
    topics          TEXT,                            -- themes / talking points
    hashtags        TEXT,                            -- preferred hashtags (use sparingly)
    cta_url         TEXT,                            -- link for the Facebook call-to-action
    language        VARCHAR(20) NOT NULL DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS media_assets (
    id          SERIAL PRIMARY KEY,
    campaign_id INT REFERENCES campaigns(id) ON DELETE SET NULL,  -- NULL = usable by any campaign
    url         TEXT NOT NULL,                                     -- public https URL (jpg/png)
    alt_text    TEXT,
    tags        TEXT,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS social_accounts (
    id                  SERIAL PRIMARY KEY,
    platform            TEXT NOT NULL CHECK (platform IN ('FACEBOOK', 'TWITTER')),
    account_name        VARCHAR(200) NOT NULL,
    platform_account_id VARCHAR(100) NOT NULL,
    -- MVP publishes with the tokens in .env / n8n credentials (encrypted by n8n).
    -- These columns exist for the multi-account phase; avoid storing live tokens
    -- here until the CMS is locked down, because NocoDB users can read them.
    access_token        TEXT,
    refresh_token       TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform, platform_account_id)
);

CREATE TABLE IF NOT EXISTS content_queue (
    id                     SERIAL PRIMARY KEY,
    campaign_id            INT REFERENCES campaigns(id) ON DELETE SET NULL,
    created_by             TEXT NOT NULL DEFAULT 'AI_AGENT'
                           CHECK (created_by IN ('AI_AGENT', 'TEAM_MEMBER', 'TWITTER_MONITOR')),
    source                 TEXT NOT NULL DEFAULT 'AI_CRON'
                           CHECK (source IN ('AI_CRON', 'MANUAL_TEAM', 'TWITTER_MONITOR')),
    fb_copy                TEXT,
    twitter_copy           TEXT,
    -- https://... public URL, or tg://<file_id> for photos sent to the bot
    -- (the publisher downloads those from Telegram at publish time).
    media_url              TEXT,
    status                 TEXT NOT NULL DEFAULT 'DRAFT'
                           CHECK (status IN ('DRAFT', 'PENDING_APPROVAL', 'APPROVED',
                                             'PUBLISHED', 'FAILED', 'REJECTED')),
    target_platform        TEXT NOT NULL DEFAULT 'ALL'
                           CHECK (target_platform IN ('ALL', 'FACEBOOK_ONLY', 'TWITTER_ONLY')),
    scheduled_at           TIMESTAMPTZ,
    published_at           TIMESTAMPTZ,
    fb_post_id             VARCHAR(100),
    tweet_id               VARCHAR(100),
    error_message          TEXT,
    regen_count            INT NOT NULL DEFAULT 0,
    approved_by            VARCHAR(100),
    telegram_chat_id       VARCHAR(50),
    telegram_message_id    VARCHAR(50),   -- preview message with the approval keyboard
    edit_prompt_message_id VARCHAR(50),   -- ForceReply prompt for the [Edit] flow
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS content_queue_status_idx ON content_queue (status);
CREATE INDEX IF NOT EXISTS content_queue_edit_prompt_idx ON content_queue (edit_prompt_message_id);

CREATE TABLE IF NOT EXISTS mentions (
    id                  SERIAL PRIMARY KEY,
    tweet_id            VARCHAR(40) NOT NULL UNIQUE,
    author_id           VARCHAR(40),
    author_username     VARCHAR(100),
    author_followers    INT,
    text                TEXT,
    tweet_created_at    TIMESTAMPTZ,
    ai_score            INT,             -- 0-10 value score from the Claude classifier
    ai_verdict          TEXT,            -- HIGH_VALUE | LOW_VALUE | SPAM | NEGATIVE | UNSAFE
    ai_reason           TEXT,
    status              TEXT NOT NULL DEFAULT 'NEW'
                        CHECK (status IN ('NEW', 'FILTERED', 'NOTIFIED', 'RETWEETING',
                                          'RETWEETED', 'IGNORED', 'FAILED')),
    telegram_message_id VARCHAR(50),
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Small key/value store for workflow cursors (e.g. Twitter since_id).
CREATE TABLE IF NOT EXISTS app_state (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Keep updated_at honest when rows are edited from NocoDB.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['campaigns', 'content_queue', 'mentions', 'app_state'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I_touch ON %I', t, t);
        EXECUTE format('CREATE TRIGGER %I_touch BEFORE UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION touch_updated_at()', t, t);
    END LOOP;
END $$;

-- Starter campaign so Workflow 1 has something to work with on day one.
-- Edit it in NocoDB (or deactivate it and add your own).
INSERT INTO campaigns (name, priority, brand_name, audience, tone_guidelines, topics, hashtags, cta_url)
SELECT 'Default Brand Campaign', 0, 'YourBrand',
       'Small-business owners who follow us on Facebook and X',
       'Friendly, confident, plain English. No hype words, no emoji walls (max 2 emoji). Never invent facts, prices or stats.',
       'Practical tips related to our product; behind-the-scenes; customer wins',
       '#YourBrand',
       'https://example.com'
WHERE NOT EXISTS (SELECT 1 FROM campaigns);
