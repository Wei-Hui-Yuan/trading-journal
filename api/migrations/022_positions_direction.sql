-- Migration 022: store a round trip's direction instead of guessing it back
--
-- The matching engine computes it exactly. `MatchedPosition.direction` is set
-- from the lot that opened the round trip -- a queue of BUY lots closed by a
-- SELL is a LONG, the reverse is a SHORT -- and then `insert_positions` threw
-- it away, because there was no column to put it in.
--
-- Both readers reconstructed it from the opening execution and guessed when
-- that lookup came back empty:
--
--     direction = (opening.direction if opening else "BUY") or "BUY"
--
-- The guess is always "long", and it is wrong in the direction that matters.
-- R degrades safely -- a short scored as a long produces negative risk and
-- reports None -- but entry slippage FLIPS SIGN: a short filled at 100 against
-- a planned 101 got in a dollar better than planned, and reads as a dollar
-- worse. The journal renders the whole round trip, and every fill in it, as a
-- long.
--
-- Not reachable today, and this is not a bugfix so much as removing the way it
-- could come back. Migration 019 made open_trade_id NOT NULL, so the lookup
-- cannot fail while that holds; the fallback is dead code that silently comes
-- alive if it ever stops holding. Verified before writing this: 0 positions
-- have a missing opening execution.
--
-- Backfill verified against the engine rather than assumed. Re-matching every
-- ticker and comparing the matcher's own direction to what this UPDATE
-- produces: 125 of 125 agree, including the single SHORT round trip in the
-- ledger. If the two disagreed anywhere, the column would be inheriting the
-- bug instead of fixing it.
--
-- LONG/SHORT rather than BUY/SELL. A position is not a buy; the opening
-- EXECUTION is. `trades.direction` keeps the broker's vocabulary and this
-- keeps the position's, which also means the value the engine already computed
-- is written verbatim, with no conversion at the point where the truth is
-- known. The API's BUY/SELL contract is unchanged -- one named mapping does
-- that at the response boundary.

BEGIN;

ALTER TABLE positions ADD COLUMN IF NOT EXISTS direction VARCHAR(5);

UPDATE positions p
   SET direction = CASE WHEN t.direction = 'BUY' THEN 'LONG' ELSE 'SHORT' END
  FROM trades t
 WHERE t.id = p.open_trade_id
   AND p.direction IS NULL;

-- Fail loudly rather than install a NOT NULL that cannot hold, so a future run
-- against different data says why instead of surfacing a constraint violation.
DO $$
DECLARE
    unresolved INTEGER;
BEGIN
    SELECT count(*) INTO unresolved FROM positions WHERE direction IS NULL;
    IF unresolved > 0 THEN
        RAISE EXCEPTION
            '% position(s) have no opening execution to take a direction from. '
            'Re-run matching for their tickers before applying this.', unresolved;
    END IF;
END $$;

ALTER TABLE positions ALTER COLUMN direction SET NOT NULL;

-- Mirrors the enum the engine uses, so a typo fails at write time rather than
-- rendering an entire round trip the wrong way round.
ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_direction_check;
ALTER TABLE positions
    ADD CONSTRAINT positions_direction_check CHECK (direction IN ('LONG', 'SHORT'));

COMMENT ON COLUMN positions.direction IS
    'LONG or SHORT, as computed by the FIFO matcher from the lot that opened '
    'the round trip. Never derived from the opening execution at read time.';

COMMIT;
