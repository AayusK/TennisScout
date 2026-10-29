import json

from tennisscout import audit, markets


def kalshi_market(yes, other, rules_tail, ticker, result):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "result": result,
            "rules_primary": f"If {yes} wins the {yes.split()[-1]} vs {other.split()[-1]} professional tennis match {rules_tail} after a ball has been played, then the market resolves to Yes."}


KALSHI = [
    kalshi_market("Ben Beta", "Alex Alpha", "in the 2025 ATP Brisbane Final", "KXATPMATCH-25JAN05BETALP-BET", "yes"),
    kalshi_market("Alex Alpha", "Ben Beta", "in the 2025 ATP Brisbane Final", "KXATPMATCH-25JAN05BETALP-ALP", "no"),
    # 2025-format wording: no tournament or round.
    kalshi_market("Alex Alpha", "Carl Gamma", "originally scheduled for Jan 3, 2024", "KXATPMATCH-24JAN03ALPGAM-ALP", "yes"),
    kalshi_market("Carl Gamma", "Alex Alpha", "originally scheduled for Jan 3, 2024", "KXATPMATCH-24JAN03ALPGAM-GAM", "no"),
    # No round, and the pair met in both Canada and Cincinnati that fortnight: ambiguous, never guessed.
    kalshi_market("Carl Gamma", "Ben Beta", "originally scheduled for Aug 8, 2025", "KXATPMATCH-25AUG08GAMBET-GAM", "yes"),
    kalshi_market("Ben Beta", "Carl Gamma", "originally scheduled for Aug 8, 2025", "KXATPMATCH-25AUG08GAMBET-BET", "no"),
    # Qualifying: must never link to the main draw.
    kalshi_market("Alex Alpha", "Ben Beta", "in the 2025 ATP Brisbane Qualification Round 1", "KXATPMATCH-25JAN01ALPBET-ALP", "yes"),
    kalshi_market("Ben Beta", "Alex Alpha", "in the 2025 ATP Brisbane Qualification Round 1", "KXATPMATCH-25JAN01ALPBET-BET", "no"),
]


def poly_event(title, start, prices):
    return {"slug": title.lower().replace(" ", "-"), "title": title, "startDate": start,
            "markets": [{"sportsMarketType": "moneyline", "gameStartTime": start.replace("T", " ").replace("Z", "+00"),
                         "clobTokenIds": json.dumps(["t1", "t2"]), "outcomePrices": json.dumps(prices)}]}


POLY = [
    poly_event("Brisbane International: Ben Beta vs Alex Alpha", "2025-01-04T02:00:00Z", ["1", "0"]),
    # Same pair, no round, dated before the main draw starts: could be qualifying, so it stays unlinked.
    poly_event("Brisbane International: Alex Alpha vs Ben Beta", "2024-12-30T02:00:00Z", ["1", "0"]),
    poly_event("Brisbane (Doubles): Alpha/Beta vs Gamma/Delta", "2025-01-04T02:00:00Z", ["1", "0"]),
]


def test_kalshi_parsing():
    evs = {e["event"]: e for e in markets.kalshi_events(KALSHI)}
    final = evs["KXATPMATCH-25JAN05BETALP"]
    assert (final["date"], final["round"], final["tournament"], final["winner"]) == ("2025-01-05", "F", "ATP Brisbane", "Ben Beta")
    assert set(final["players"]) == {"Ben Beta", "Alex Alpha"}
    assert evs["KXATPMATCH-25AUG08GAMBET"]["round"] is None
    assert evs["KXATPMATCH-25JAN01ALPBET"]["round"] == "Q"


def test_polymarket_parsing_skips_doubles():
    evs = markets.polymarket_events(POLY)
    assert len(evs) == 2
    assert evs[0]["tournament"] == "Brisbane International"
    assert evs[0]["winner"] == "Ben Beta"
    assert evs[0]["scheduled_start"] == "2025-01-04T02:00:00Z"


def test_market_coverage_links_and_guards(loaded, monkeypatch):
    conn, *_ = loaded
    monkeypatch.setattr(markets, "load_listing", lambda name: {
        "collected_at": "2026-10-29T00:00:00Z", "rows": KALSHI if name.startswith("kalshi") else POLY})
    cov = audit.market_coverage(conn)
    k, p = cov["per_exchange"]["kalshi"], cov["per_exchange"]["polymarket"]
    # The final and the round-less Jan 2024 match link; the qualifying market does not; Aug 8 stays ambiguous.
    assert k["status"] == {"linked": 2, "no_tour_match": 1, "ambiguous": 1}
    assert k["settled_winner_vs_tml"] == {"agrees": 2}
    # The pre-main-draw Polymarket event stays unlinked.
    assert p["status"]["linked"] == 1 and p["status"]["no_tour_match"] == 1


def test_kalshi_fee_and_checkpoint_lookup():
    from tennisscout.market_eval import at, kalshi_fee

    assert kalshi_fee(0.5) == 0.02  # 0.07 * 0.25 = 0.0175 -> 2 cents
    assert kalshi_fee(0.9) == 0.01
    series = [(100, 0.4), (200, 0.5), (300, 0.6)]
    assert at(series, 250) == (200, 0.5)
    assert at(series, 50) is None


def test_trading_counts_edge_after_fee():
    from tennisscout import market_eval

    rows = [{"model": 0.7, "y": 1, "kalshi_ask_p1": 0.5, "kalshi_ask_p2": 0.52},
            {"model": 0.7, "y": 0, "kalshi_ask_p1": 0.5, "kalshi_ask_p2": 0.52},
            {"model": 0.5, "y": 1, "kalshi_ask_p1": 0.5, "kalshi_ask_p2": 0.52}]  # no edge on the last row
    t = market_eval.trading(rows)["edge>=0.1"]
    fee = market_eval.kalshi_fee(0.5)
    assert t["bets"] == 2 and abs(t["pnl_per_contract_sum"] - round((1 - 0.5 - fee) + (0 - 0.5 - fee), 2)) < 1e-9
    assert t["pnl_se"] > 0
