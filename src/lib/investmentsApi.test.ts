/**
 * The investment book's HTTP layer: which verb, which URL, which body.
 *
 * Nineteen functions and almost all of them are one line -- call `apiClient`,
 * return `data`. Testing that is mostly mechanical, which is exactly why it is
 * worth doing: a wrapper this thin has no logic to protect it from a copy-paste
 * mistake (POST where PATCH was meant, the wrong path segment), and nothing
 * else in the app would catch that until a click failed in a browser.
 *
 * `apiClient` is mocked wholesale rather than mocking HTTP at the network
 * layer -- this module's only job is "did it ask apiClient for the right
 * thing", and axios's own request handling is not what is under test here.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiClient } from '@/lib/api';
import type { HoldingPayload, TransactionPayload } from '@/types/investments';
import {
  clearValuationOverride,
  correctBasis,
  createHolding,
  createInvestmentTransaction,
  deleteHolding,
  deleteInvestmentTransaction,
  deleteSectorColor,
  getInvestmentTransactions,
  getPortfolio,
  getSectorColors,
  refreshPrices,
  refreshValuations,
  setSectorColor,
  setValuationOverride,
  syncTransactions,
  updateHolding,
  updateInvestmentTransaction,
} from '@/lib/investmentsApi';

vi.mock('@/lib/api', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

const mocked = apiClient as unknown as {
  get: ReturnType<typeof vi.fn>;
  post: ReturnType<typeof vi.fn>;
  patch: ReturnType<typeof vi.fn>;
  put: ReturnType<typeof vi.fn>;
  delete: ReturnType<typeof vi.fn>;
};

/** The shape every mocked verb resolves with: axios wraps the body in `data`. */
const respond = (data: unknown) => ({ data });

beforeEach(() => {
  mocked.get.mockReset().mockResolvedValue(respond(null));
  mocked.post.mockReset().mockResolvedValue(respond(null));
  mocked.patch.mockReset().mockResolvedValue(respond(null));
  mocked.put.mockReset().mockResolvedValue(respond(null));
  mocked.delete.mockReset().mockResolvedValue(respond(undefined));
});

describe('getPortfolio', () => {
  it('GETs the portfolio endpoint and returns the body', async () => {
    const portfolio = { holdings: [], total_market_value: 0 };
    mocked.get.mockResolvedValueOnce(respond(portfolio));

    await expect(getPortfolio()).resolves.toBe(portfolio);
    expect(mocked.get).toHaveBeenCalledWith('/investments/portfolio');
  });
});

describe('getInvestmentTransactions', () => {
  it('omits params entirely when no ticker is given', async () => {
    // Not `{ ticker: undefined }` -- axios serialises that as `?ticker=`, which
    // the endpoint would have to specifically null-check for. `undefined` for
    // the whole params object is what makes "no filter" and "filtered by
    // nothing" the same request.
    await getInvestmentTransactions();

    expect(mocked.get).toHaveBeenCalledWith('/investments/transactions', {
      params: undefined,
    });
  });

  it('scopes to one ticker when given', async () => {
    await getInvestmentTransactions('VOO');

    expect(mocked.get).toHaveBeenCalledWith('/investments/transactions', {
      params: { ticker: 'VOO' },
    });
  });
});

describe('createInvestmentTransaction', () => {
  it('POSTs the payload and returns the created row', async () => {
    const created = { id: 't1', ticker: 'VOO' };
    mocked.post.mockResolvedValueOnce(respond(created));
    const payload: TransactionPayload = {
      ticker: 'VOO',
      transaction_type: 'BUY',
      transaction_date: '2026-08-01T00:00:00Z',
    };

    await expect(createInvestmentTransaction(payload)).resolves.toBe(created);
    expect(mocked.post).toHaveBeenCalledWith('/investments/transactions', payload);
  });
});

describe('updateInvestmentTransaction', () => {
  it('PATCHes the one transaction by id', async () => {
    await updateInvestmentTransaction('t1', { note: 'corrected' });

    expect(mocked.patch).toHaveBeenCalledWith('/investments/transactions/t1', {
      note: 'corrected',
    });
  });
});

describe('deleteInvestmentTransaction', () => {
  it('DELETEs the one transaction by id, returning nothing', async () => {
    await expect(deleteInvestmentTransaction('t1')).resolves.toBeUndefined();
    expect(mocked.delete).toHaveBeenCalledWith('/investments/transactions/t1');
  });
});

describe('createHolding', () => {
  it('POSTs to the holdings collection', async () => {
    const payload: HoldingPayload = { ticker: 'VOO', category: 'ETF' };
    await createHolding(payload);

    expect(mocked.post).toHaveBeenCalledWith('/investments/holdings', payload);
  });
});

describe('updateHolding', () => {
  it('PATCHes one holding by ticker, accepting a partial payload', async () => {
    // Partial<HoldingPayload> at the type level; asserted here so a change
    // that widens the payload back to a full object is caught.
    await updateHolding('VOO', { sector: 'Technology' });

    expect(mocked.patch).toHaveBeenCalledWith('/investments/holdings/VOO', {
      sector: 'Technology',
    });
  });
});

describe('deleteHolding', () => {
  it('DELETEs one holding by ticker', async () => {
    await deleteHolding('VOO');

    expect(mocked.delete).toHaveBeenCalledWith('/investments/holdings/VOO');
  });
});

describe('correctBasis', () => {
  it('POSTs -- an action with a ledger side effect, not a PATCH', async () => {
    // The doc comment is explicit that this writes an ADJUSTMENT transaction
    // rather than editing a field, which is exactly why the verb matters here.
    const payload = { quantity: 10, average_cost: 405.1 };
    await correctBasis('VOO', payload);

    expect(mocked.post).toHaveBeenCalledWith(
      '/investments/holdings/VOO/basis-correction',
      payload
    );
    expect(mocked.patch).not.toHaveBeenCalled();
  });
});

describe('setValuationOverride', () => {
  it('PUTs the whole input set, not a partial', async () => {
    // The doc comment says PUT specifically so "cleared" and "untouched" stay
    // distinguishable -- a PATCH would collapse that distinction.
    const payload = { base_flow: 1000 };
    await setValuationOverride('VOO', payload);

    expect(mocked.put).toHaveBeenCalledWith(
      '/investments/holdings/VOO/valuation-override',
      payload
    );
  });
});

describe('clearValuationOverride', () => {
  it('DELETEs the override, returning nothing', async () => {
    await expect(clearValuationOverride('VOO')).resolves.toBeUndefined();
    expect(mocked.delete).toHaveBeenCalledWith(
      '/investments/holdings/VOO/valuation-override'
    );
  });
});

describe('refreshPrices', () => {
  it('POSTs with a null body and a 120s timeout', async () => {
    await refreshPrices();

    expect(mocked.post).toHaveBeenCalledWith(
      '/investments/refresh-prices',
      null,
      { timeout: 120_000 }
    );
  });
});

describe('refreshValuations', () => {
  it('defaults force to false', async () => {
    await refreshValuations();

    expect(mocked.post).toHaveBeenCalledWith('/investments/refresh', null, {
      params: { force: false },
      timeout: 300_000,
    });
  });

  it('passes force through when the caller asks for it explicitly', async () => {
    await refreshValuations(true);

    expect(mocked.post).toHaveBeenCalledWith('/investments/refresh', null, {
      params: { force: true },
      timeout: 300_000,
    });
  });
});

describe('syncTransactions', () => {
  it('POSTs with the same generous timeout the Flex handshake needs', async () => {
    await syncTransactions();

    expect(mocked.post).toHaveBeenCalledWith('/investments/sync', null, {
      timeout: 300_000,
    });
  });
});

describe('getSectorColors', () => {
  it('GETs the sparse override list', async () => {
    await getSectorColors();

    expect(mocked.get).toHaveBeenCalledWith('/investments/sector-colors');
  });
});

describe('setSectorColor', () => {
  it('percent-encodes the sector name in the URL', async () => {
    // "Consumer Discretionary" is a real sector name with a space in it --
    // an un-encoded path segment would be a malformed request, not a typo
    // that fails loudly.
    await setSectorColor('Consumer Discretionary', '#ff8800');

    expect(mocked.put).toHaveBeenCalledWith(
      '/investments/sector-colors/Consumer%20Discretionary',
      { color: '#ff8800' }
    );
  });

  it('encodes a slash in a sector name, not just spaces', async () => {
    // A generic "replace spaces" implementation would still send a broken
    // path for this one; encodeURIComponent handles every reserved character.
    await setSectorColor('Oil & Gas / Energy', '#000000');

    expect(mocked.put).toHaveBeenCalledWith(
      `/investments/sector-colors/${encodeURIComponent('Oil & Gas / Energy')}`,
      { color: '#000000' }
    );
  });
});

describe('deleteSectorColor', () => {
  it('percent-encodes the sector name and returns nothing', async () => {
    await expect(deleteSectorColor('Consumer Discretionary')).resolves.toBeUndefined();

    expect(mocked.delete).toHaveBeenCalledWith(
      '/investments/sector-colors/Consumer%20Discretionary'
    );
  });
});
