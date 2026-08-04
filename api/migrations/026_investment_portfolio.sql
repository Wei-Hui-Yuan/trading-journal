-- Migration 026: the long-term book, kept entirely apart from the trading one.
--
-- Nothing in this file touches `trades`, `positions`, `position_fills`,
-- `realized_legs` or `planned_trades`. There is no ALTER on an existing
-- table and no foreign key pointing into one. Every statement is a CREATE
-- ... IF NOT EXISTS, so applying this to a database that already has it is a
-- no-op, and applying it to one that does not cannot disturb what is there.
--
-- WHY SEPARATE TABLES AT ALL. `positions` is built around an idea opened and
-- closed: FIFO round trips, R-multiples, entry slippage, a discipline
-- checklist. None of that means anything for a holding bought monthly and
-- kept for a decade. Forcing both through one schema would corrupt the
-- trading side, which works and has 533 tests behind it, to serve a domain
-- that does not want its shape.
--
-- THREE TABLES, NOT TWO, and the split is deliberate:
--
--   investment_transactions  what happened  (the immutable ledger)
--   investment_holdings      what you hold  (classification + last price)
--   investment_valuation_inputs  what the DCF was fed
--
-- Quantity and average cost are NOT stored. They are derived from the
-- transaction ledger on read, the same way the trading side derives
-- positions from fills -- because a stored aggregate is free to drift from
-- the rows beneath it, and this codebase has already paid for that lesson
-- once (see the authoritative-matching work in 022).


-- ---------------------------------------------------------------------------
-- 1. The ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS investment_transactions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    ticker              VARCHAR(20)  NOT NULL,

    -- DIVIDEND carries no quantity or price, only a total. TRANSFER moves
    -- shares in without a purchase price, which is how a legacy holding
    -- arrives from another broker with its cost basis intact.
    transaction_type    VARCHAR(12)  NOT NULL,

    quantity            NUMERIC(18, 8),
    price               NUMERIC(18, 4),

    -- Signed from the account's point of view: negative when money left to
    -- buy something, positive when it arrived. Stated here rather than left
    -- to a convention, because an unsigned amount plus a type is the same
    -- fact stored twice and free to disagree.
    total_amount        NUMERIC(18, 4) NOT NULL,

    fees                NUMERIC(18, 4) NOT NULL DEFAULT 0,

    transaction_date    TIMESTAMPTZ  NOT NULL,

    -- The currency this row's money is denominated in, and what one USD was
    -- worth in it at the time. Recorded per transaction rather than looked
    -- up later: a purchase made at 7.80 HKD/USD stays a purchase made at
    -- 7.80 however the rate moves afterwards.
    listed_currency     VARCHAR(3)   NOT NULL DEFAULT 'USD',
    exchange_rate       NUMERIC(18, 8) NOT NULL DEFAULT 1,

    -- MANUAL or IBKR. The broker path is not built yet; the column exists so
    -- that adding it later does not require a migration against a table that
    -- by then holds real history.
    source              VARCHAR(12)  NOT NULL DEFAULT 'MANUAL',

    -- Deduplication key for any future broker sync. Deliberately nullable
    -- and UNIQUE: Postgres treats NULLs as distinct, so hand-entered rows
    -- never collide with each other, while a broker id can only land once.
    external_id         TEXT UNIQUE,

    note                TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT investment_transactions_type_check
        CHECK (transaction_type IN ('BUY', 'SELL', 'DIVIDEND', 'TRANSFER')),

    -- A BUY or SELL without a quantity is not a trade, it is a typo. A
    -- DIVIDEND with one is a share dividend, which this does not model yet
    -- and should fail loudly rather than be counted as a purchase.
    CONSTRAINT investment_transactions_quantity_check
        CHECK (
            (transaction_type IN ('BUY', 'SELL', 'TRANSFER')
             AND quantity IS NOT NULL AND quantity > 0)
            OR
            (transaction_type = 'DIVIDEND' AND quantity IS NULL)
        ),

    CONSTRAINT investment_transactions_rate_check
        CHECK (exchange_rate > 0),

    CONSTRAINT investment_transactions_source_check
        CHECK (source IN ('MANUAL', 'IBKR'))
);

-- The two questions asked of this table: everything for one ticker (to
-- derive its position), and everything recent (the audit log).
CREATE INDEX IF NOT EXISTS ix_investment_tx_ticker
    ON investment_transactions (ticker, transaction_date);
CREATE INDEX IF NOT EXISTS ix_investment_tx_date
    ON investment_transactions (transaction_date DESC);


-- ---------------------------------------------------------------------------
-- 2. The holdings, and how they are classified
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS investment_holdings (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- One book, so the ticker is the identity. No account column: the
    -- portfolio sheet this replaces has a single account, and a column
    -- holding one repeated value forever is a column that lies about the
    -- shape of the data. Adding one later is an ALTER against a small table.
    ticker              VARCHAR(20)  NOT NULL UNIQUE,
    name                TEXT,

    -- Sector routes the valuation method and is fetched. The other three are
    -- the user's own taxonomy from the portfolio sheet, and are theirs to
    -- set -- Growth/Predictable/ETF, Moderate Cyclical/Defensive, and where
    -- it is listed.
    sector              TEXT,
    category            VARCHAR(20),
    holding_type        VARCHAR(24),
    country             VARCHAR(32),

    listed_currency     VARCHAR(3)   NOT NULL DEFAULT 'USD',
    exchange_rate       NUMERIC(18, 8) NOT NULL DEFAULT 1,

    -- What you intend to put into this name, in its listed currency. The
    -- sheet sizes in absolute money rather than target percentages, and the
    -- DCA helper reads this.
    planned_allocation  NUMERIC(18, 4),

    -- Whether a discounted cash flow means anything here. False for an ETF,
    -- which has no cash flows of its own -- roughly a quarter of this
    -- portfolio. Stored rather than inferred from `category` so that a fund
    -- miscategorised once does not silently acquire an intrinsic value.
    is_valuable         BOOLEAN      NOT NULL DEFAULT TRUE,

    -- Last quote, and when. Refreshed daily; every figure derived from it
    -- carries this timestamp so the UI can say how stale it is instead of
    -- implying it is live.
    current_price       NUMERIC(18, 4),
    price_updated_at    TIMESTAMPTZ,

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT investment_holdings_rate_check CHECK (exchange_rate > 0),
    CONSTRAINT investment_holdings_category_check
        CHECK (category IS NULL OR category IN ('Growth', 'Predictable', 'ETF'))
);


-- ---------------------------------------------------------------------------
-- 3. What the model was fed
-- ---------------------------------------------------------------------------
--
-- Two rows per ticker at most, discriminated by `variant`:
--
--   'auto'      what FMP and GuruFocus returned, replaced wholesale on each
--               monthly refresh
--   'override'  what the user typed in the modal, never touched by a refresh
--
-- That separation is the entire point. A refresh must be able to update the
-- baseline without erasing a judgement the user made deliberately, and the
-- two must be displayable side by side so a drifting assumption is visible
-- rather than silently overwritten. The same idea as `trades.broker_original`
-- on the trading side: keep the machine's copy and the human's correction
-- distinguishable forever.
--
-- Every input is nullable in the 'override' row -- an override sets one
-- field, not all of them, and a NULL means "fall back to auto for this one".
--
-- The intrinsic values themselves are NOT stored. They are computed on read
-- by services/valuation.py, which is pure arithmetic over these columns and
-- costs microseconds. Storing them would create a third thing that can
-- disagree with the two above it, and the monthly cadence applies to
-- FETCHING inputs, not to doing the sum.
CREATE TABLE IF NOT EXISTS investment_valuation_inputs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    ticker              VARCHAR(20)  NOT NULL,
    variant             VARCHAR(10)  NOT NULL,

    -- The flow being grown: free cash flow, operating cash flow or net
    -- income depending on method. `metric` records which, so a stored
    -- valuation can say what it valued rather than leaving it to be guessed.
    base_flow           NUMERIC(20, 4),
    metric              VARCHAR(24),

    shares_outstanding  NUMERIC(20, 4),
    total_debt          NUMERIC(20, 4),
    cash_and_st         NUMERIC(20, 4),

    beta                NUMERIC(10, 4),
    -- Years 1-5. The only growth figure that comes from data; years 6-10 and
    -- 11-20 are rules applied to it inside the engine.
    growth_1_5          NUMERIC(10, 6),

    -- Set only when the user pins a rate by hand; otherwise the engine
    -- derives it from beta and region.
    discount_rate       NUMERIC(10, 6),
    region              VARCHAR(4)   NOT NULL DEFAULT 'US',

    -- Which provider each refresh came from, for when two disagree.
    source              VARCHAR(24),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT investment_valuation_inputs_variant_check
        CHECK (variant IN ('auto', 'override')),
    CONSTRAINT investment_valuation_inputs_region_check
        CHECK (region IN ('US', 'HK')),
    -- One auto row and one override row per ticker. This is what the refresh
    -- upserts against, and what stops a second refresh appending rather than
    -- replacing.
    CONSTRAINT uq_investment_valuation_inputs UNIQUE (ticker, variant)
);

CREATE INDEX IF NOT EXISTS ix_investment_valuation_ticker
    ON investment_valuation_inputs (ticker);
