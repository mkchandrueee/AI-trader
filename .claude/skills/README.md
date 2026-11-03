# Trader skills — Indian markets (NSE/BSE)

Eleven skills covering one trade end to end, adapted from
[Jakeschincariol/day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT), which targets US markets. They are **not** a port: the US original reads
Yahoo US charts, Nasdaq symbol lists, SEC EDGAR and the House Clerk, none of
which apply to NSE. These wrap this platform's own modules instead, so a skill
and the live agent can never disagree about what a signal or a limit means.

| skill | wraps | needs |
| --- | --- | --- |
| `trader-scanner` | `strategy/intraday_scanner.py` | cached bars |
| `trader-news` | `data/news_sentiment.py` | internet (free RSS) |
| `trader-entry` | `strategy/premarket.live_confirmation` | **live AngelOne session + market hours** |
| `trader-exit` | `strategy/math_decision_strategy.py` | nothing |
| `trader-timemachine` | `data/intraday_bars_store` + `config/measured_costs` | cached bars |
| `trader-risk` | `risk/pretrade.py` | nothing (`--live` reads the backend) |
| `trader-budget` | `config/risk_profiles.py` | nothing |
| `trader-broker` | `risk/pretrade.py` | nothing — **sends nothing, ever** |
| `trader-alerts` | the collector's tick cache | cache file |
| `trader-analyst` | news feed + `data/eod_store.py` | internet |
| `trader-sebi` | SEBI RSS | internet |

## The pipeline for one trade

```
scanner → news → entry → exit → timemachine → budget → risk → broker
                                                      alerts (while open)
```

`news`, `analyst` and `sebi` are also worth running on their own schedule.

## What could not be built, and why

Checked before writing anything, on 2026-11-03:

| wanted | source | result |
| --- | --- | --- |
| insider / promoter disclosures | `nseindia.com/api/corporates-pit` | 200 but `{"acqNameList":[],"data":[]}` — Akamai-gated |
| corporate announcements | `api.bseindia.com/.../AnnGetData` | HTTP 403 |
| regulatory actions | `sebi.gov.in/sebirss.xml` | **200, ~14KB — used** |

So there is no Indian equivalent of `trader-pelosi`: MPs' trades are not
disclosed here, and the exchange insider feeds are not reachable from a script.
`trader-sebi` is the honest substitute — it names the scrips under regulatory
action, which is arguably more useful to a trader anyway.

For the same reason `trader-analyst` reports results **headlines plus a measured
price reaction** rather than a filings summary. There is no EPS or revenue
figure available to quote, and it says so on every run.

Equity slippage in `trader-timemachine` is **assumed, not measured** — this
platform collects no equity quotes, only NIFTY options and futures. Option costs
elsewhere are measured (0.237% round trip from 1.8M of our own bid/ask quotes).

## Safety, which is not boilerplate here

- **Nothing places an order.** `trader-broker` builds the ticket, runs the gate,
  and stops; it has no code path to a broker. Paper is the default, live orders
  go through `models/approval.py` and a human in the dashboard, and the agent
  additionally needs `INTRADAY_AGENT_LIVE_AUTOFIRE_CONFIRMED` before it will
  auto-fire anything.
- **Exits are never blocked** by the risk gate. A gate that can trap you in a
  position is worse than no gate.
- **The strategies have no measured edge.** The option engine is in
  `PAPER_TESTING` with a profit factor of 0.52 over 78 trades and a recorded
  out-of-sample backtest of −7.38% of premium per trade; the intraday patterns
  found 0 of 240 configurations profitable on either split. These skills help you
  see and control what the platform does. They are not evidence that it works —
  the Strategy Lab (`/lab`) is where that question is answered.
- `trader-entry` makes real broker calls. AngelOne refused ~half of this
  platform's historical calls when demand spiked, and each refusal costs the live
  agent a decision, so it floors its polling interval at 30s and never retries a
  refusal.
