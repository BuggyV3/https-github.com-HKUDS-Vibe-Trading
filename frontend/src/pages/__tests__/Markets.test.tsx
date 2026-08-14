import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { Markets } from "../Markets";
import type { OptionChainResponse, ScreenerResponse } from "@/lib/api";

const apiMock = vi.hoisted(() => ({
  getMarketScreener: vi.fn(),
  getOptionChain: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("@/components/charts/IvSmileChart", () => ({
  IvSmileChart: ({ spot }: { spot: number | null }) => (
    <div data-testid="iv-smile">{spot ?? "no-spot"}</div>
  ),
}));

function screener(overrides: Partial<ScreenerResponse> = {}): ScreenerResponse {
  return {
    market: "us",
    sort_by: "amount",
    top_n: 40,
    source: "eastmoney",
    as_of: new Date().toISOString(),
    weights: { liquidity: 0.4, activity: 0.3, momentum: 0.3 },
    rows: [
      {
        code: "AAPL",
        name: "Apple",
        price: 200,
        change_pct: 1.5,
        change: 3,
        volume: 5e7,
        amount: 1e10,
        turnover_rate: 0.4,
        liquidity_pct: 0,
        activity_pct: 0,
        momentum_pct: 0,
        flow_score: 10,
      },
      {
        code: "TSLA",
        name: "Tesla",
        price: 300,
        change_pct: -4,
        change: -12,
        volume: 9e7,
        amount: 2e10,
        turnover_rate: 1.2,
        liquidity_pct: 100,
        activity_pct: 100,
        momentum_pct: 100,
        flow_score: 90,
      },
    ],
    ...overrides,
  };
}

function chain(overrides: Partial<OptionChainResponse> = {}): OptionChainResponse {
  return {
    ticker: "AAPL",
    spot: 150,
    currency: "USD",
    expiration: 1800000000,
    expirations: [1800000000, 1800604800],
    days_to_expiry: 21,
    risk_free_rate: 0.04,
    as_of: new Date().toISOString(),
    source: "yahoo",
    calls: [
      {
        contract_symbol: "AAPL_C150",
        side: "call",
        strike: 150,
        last_price: 4.1,
        bid: 4,
        ask: 4.2,
        mid: 4.1,
        spread: 0.2,
        spread_pct: 0.048,
        volume: 900,
        open_interest: 4000,
        volume_oi_ratio: 0.22,
        implied_volatility: 0.28,
        iv_source: "provider",
        in_the_money: false,
        moneyness: 0,
        break_even: 154.1,
        notional: 410,
        days_to_expiry: 21,
        greeks: { delta: 0.53, gamma: 0.02, theta: -0.08, vega: 0.17, rho: 0.05 },
        liquidity_score: 88,
        expiration: 1800000000,
      },
    ],
    puts: [
      {
        contract_symbol: "AAPL_P150",
        side: "put",
        strike: 150,
        last_price: 3.6,
        bid: 3.5,
        ask: 3.7,
        mid: 3.6,
        spread: 0.2,
        spread_pct: 0.055,
        volume: 400,
        open_interest: 2500,
        volume_oi_ratio: 0.16,
        implied_volatility: null,
        iv_source: null,
        in_the_money: false,
        moneyness: 0,
        break_even: 146.4,
        notional: 360,
        days_to_expiry: 21,
        greeks: null,
        liquidity_score: 71,
        expiration: 1800000000,
      },
    ],
    summary: {
      call_open_interest: 4000,
      put_open_interest: 2500,
      call_volume: 900,
      put_volume: 400,
      put_call_oi_ratio: 0.625,
      put_call_volume_ratio: 0.444,
      atm_iv: 0.29,
      median_spread_pct: 0.051,
      contracts: 2,
    },
    ...overrides,
  };
}

describe("Markets dashboard", () => {
  beforeEach(() => {
    apiMock.getMarketScreener.mockReset();
    apiMock.getOptionChain.mockReset();
    apiMock.getMarketScreener.mockResolvedValue(screener());
    apiMock.getOptionChain.mockResolvedValue(chain());
  });

  it("renders the ranked rows returned by the screener", async () => {
    render(<Markets />);

    expect(await screen.findByText("AAPL")).toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(apiMock.getMarketScreener).toHaveBeenCalledWith(
      "us",
      "amount",
      40,
      expect.anything(),
    );
  });

  it("refetches when the market or ranking metric changes", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");

    fireEvent.click(screen.getByRole("button", { name: "Hong Kong" }));

    await waitFor(() =>
      expect(apiMock.getMarketScreener).toHaveBeenLastCalledWith(
        "hk",
        "amount",
        40,
        expect.anything(),
      ),
    );
  });

  it("filters rows locally without issuing another request", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");
    const callCount = apiMock.getMarketScreener.mock.calls.length;

    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "tesla" } });

    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(apiMock.getMarketScreener).toHaveBeenCalledTimes(callCount);
  });

  it("keeps a direction filter honest about missing rows", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");

    fireEvent.click(screen.getByRole("button", { name: "Down" }));

    expect(screen.getByText("TSLA")).toBeInTheDocument();
    expect(screen.queryByText("AAPL")).not.toBeInTheDocument();
  });

  it("loads the option chain for a clicked row", async () => {
    render(<Markets />);
    fireEvent.click(await screen.findByText("AAPL"));

    await waitFor(() =>
      expect(apiMock.getOptionChain).toHaveBeenCalledWith(
        "AAPL",
        null,
        undefined,
        expect.anything(),
      ),
    );
    expect(await screen.findByTestId("iv-smile")).toHaveTextContent("150");
  });

  it("requests the chosen expiration without resetting the symbol", async () => {
    render(<Markets />);
    fireEvent.click(await screen.findByText("AAPL"));
    await screen.findByTestId("iv-smile");

    // 1800604800 renders as its UTC date in the expiration ladder.
    fireEvent.click(screen.getByRole("button", { name: "2027-01-22" }));

    await waitFor(() =>
      expect(apiMock.getOptionChain).toHaveBeenLastCalledWith(
        "AAPL",
        1800604800,
        undefined,
        expect.anything(),
      ),
    );
  });

  it("renders a dash rather than a zero where the provider had no value", async () => {
    render(<Markets />);
    fireEvent.click(await screen.findByText("AAPL"));
    await screen.findByTestId("iv-smile");

    // The put row carries no IV and no greeks; those cells must read as absent.
    // The break-even also appears on the shortlist card, so scope to the ladder.
    const putRow = screen
      .getAllByText("146.40")
      .map((node) => node.closest("tr"))
      .find((row): row is HTMLTableRowElement => row !== null);
    expect(putRow).toBeDefined();
    const cells = within(putRow as HTMLElement).getAllByRole("cell");
    expect(cells.map((cell) => cell.textContent)).toContain("—");
  });

  it("surfaces a screener failure without blanking the rows already shown", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");

    apiMock.getMarketScreener.mockRejectedValueOnce(new Error("upstream down"));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));

    expect(await screen.findByText("upstream down")).toBeInTheDocument();
    expect(screen.getByText("AAPL")).toBeInTheDocument();
  });

  it("reports a chain failure in place of a stale chain", async () => {
    apiMock.getOptionChain.mockRejectedValueOnce(new Error("yahoo 502"));
    render(<Markets />);

    fireEvent.click(await screen.findByText("AAPL"));

    expect(await screen.findByText("yahoo 502")).toBeInTheDocument();
    expect(screen.queryByTestId("iv-smile")).not.toBeInTheDocument();
  });

  it("says option chains are US-only on a non-US market", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");

    fireEvent.click(screen.getByRole("button", { name: "A-share" }));

    expect(
      await screen.findByText(/Option chains cover US-listed underlyings only/),
    ).toBeInTheDocument();
  });

  it("discloses how the scores are built", async () => {
    render(<Markets />);
    await screen.findByText("AAPL");

    expect(screen.getByText(/not forecasts, recommendations or investment advice/)).toBeInTheDocument();
  });
});
