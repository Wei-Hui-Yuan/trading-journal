-- Migration 025: attach the chart you were looking at to the plan you wrote.
--
-- A plan records the levels and the thesis, which is what you INTENDED. It
-- could not record what you were looking at when you decided -- and for a
-- discretionary setup that is most of the reason. "Reclaiming the 50 EMA
-- after basing three days" is a sentence; whether the base was actually there
-- is a picture, and reviewing the trade later without it means grading the
-- sentence rather than the decision.
--
-- The bytes live in Supabase Storage, not here. Postgres would hold them
-- fine -- a lossless WebP of a 1866x1244 chart measures well under a
-- megabyte -- but the free tier's database allowance is 500 MB against
-- Storage's 1 GB, and the database one is shared with every table that
-- actually gets queried. Images are never joined, filtered or aggregated;
-- they are fetched whole, by key, exactly once per view. That is what object
-- storage is, so they go there and this table keeps the key.
--
-- Four columns rather than one, because "there is an image" and "the image is
-- this many bytes" are both questions the app asks without wanting to touch
-- Storage: the dock decides whether to render a thumbnail slot, and the
-- storage figure is reportable without an API round trip per plan.

ALTER TABLE planned_trades
    -- Object path within the bucket, e.g. 'plan-charts/<plan-uuid>.webp'.
    -- Not a full URL: the bucket is private, so what the browser eventually
    -- loads is minted per request and would be stale the moment it was stored.
    ADD COLUMN IF NOT EXISTS chart_path        TEXT,
    ADD COLUMN IF NOT EXISTS chart_mime        VARCHAR(32),
    ADD COLUMN IF NOT EXISTS chart_bytes       INTEGER,
    ADD COLUMN IF NOT EXISTS chart_uploaded_at TIMESTAMPTZ;

-- All four together or none at all.
--
-- The failure this prevents is a half-written attachment: an upload that
-- stored the path but died before the size, leaving a plan that claims an
-- image whose bytes nobody can account for, or a size with no object behind
-- it. Both read as "there is a chart" to anything checking a single column,
-- and the endpoint checks chart_path.
--
-- Wrapped because ADD CONSTRAINT has no IF NOT EXISTS, and a migration that
-- cannot be re-run is a migration you are afraid to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_trades_chart_complete_check'
    ) THEN
        ALTER TABLE planned_trades
            ADD CONSTRAINT planned_trades_chart_complete_check
            CHECK (
                (chart_path IS NULL
                 AND chart_mime IS NULL
                 AND chart_bytes IS NULL
                 AND chart_uploaded_at IS NULL)
                OR
                (chart_path IS NOT NULL
                 AND chart_mime IS NOT NULL
                 AND chart_bytes IS NOT NULL
                 AND chart_uploaded_at IS NOT NULL)
            );
    END IF;
END $$;

-- A stored size must be a real size. Zero bytes is not a small image, it is a
-- failed upload that reported success.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_trades_chart_bytes_check'
    ) THEN
        ALTER TABLE planned_trades
            ADD CONSTRAINT planned_trades_chart_bytes_check
            CHECK (chart_bytes IS NULL OR chart_bytes > 0);
    END IF;
END $$;

-- Only the browser formats the uploader actually produces. WebP is what it
-- emits (lossless, and measurably the smallest for flat-background chart UI);
-- PNG is the fallback for a browser without WebP encode support. Anything
-- else arriving here means the client sent something the compressor never
-- made, which is worth failing on rather than storing.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'planned_trades_chart_mime_check'
    ) THEN
        ALTER TABLE planned_trades
            ADD CONSTRAINT planned_trades_chart_mime_check
            CHECK (chart_mime IS NULL OR chart_mime IN ('image/webp', 'image/png'));
    END IF;
END $$;
