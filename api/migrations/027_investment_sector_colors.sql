-- Migration 027: manual color overrides for the sector treemap
--
-- AllocationPanel colors every treemap tile and legend swatch from a fixed
-- palette (GROUP_COLORS) keyed by sector name in the frontend. The palette is
-- deliberately desaturated so it sits behind the numbers rather than
-- competing with them, but that same restraint means several sectors can
-- land in the same blue-gray family and stop being distinguishable at a
-- glance -- the "colours are too similar" problem this migration exists to
-- fix, by letting the trader pick a color per sector by hand.
--
-- A manual pick belongs in the database rather than localStorage, for the
-- same reason timeframe_presets does (see migration 021): a color scheme
-- chosen on the desktop should still be there on the phone, and two tabs
-- recoloring different sectors at once should not risk one silently
-- clobbering the other under a read-modify-write of a single JSON blob.
--
-- Keyed by the sector NAME itself, not a foreign key -- there is no sectors
-- table this could reference. The same string is also what a holding with no
-- sector falls back to (its category, or "No Target Set"), so a colored
-- fallback bucket is just another row here, nothing special-cased.
--
-- Sectors with no row here simply use the built-in palette; this table only
-- ever holds the ones the trader has deliberately recolored.

BEGIN;

CREATE TABLE IF NOT EXISTS investment_sector_colors (
    sector      TEXT PRIMARY KEY,
    color       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT investment_sector_colors_name_not_blank
        CHECK (btrim(sector) <> ''),

    -- #RRGGBB -- what <input type="color"> and CSS backgroundColor both take
    -- natively, so nothing needs translating between storage and render.
    CONSTRAINT investment_sector_colors_hex
        CHECK (color ~ '^#[0-9A-Fa-f]{6}$')
);

COMMENT ON TABLE investment_sector_colors IS
    'Manual color overrides for the sector treemap, one row per sector name '
    'the trader has recolored by hand. A sector with no row uses the '
    'built-in palette.';

COMMIT;
