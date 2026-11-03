---
name: trader-sebi
description: >-
  Lists SEBI regulatory actions naming listed Indian companies — adjudication and
  settlement orders, front-running and market-manipulation cases, recovery
  certificates. Use when asked whether a stock is under regulatory action, about
  SEBI orders or enforcement, or to check a scrip before trading it. The Indian
  stand-in for the US congressional-disclosure skill.
argument-hint: "[--symbol NAME --kind adjudication]"
---

# trader-sebi

The US original reads congressional trade disclosures from the House Clerk.
**India publishes no equivalent** — MPs' trades are not disclosed — so that skill
cannot be ported, and pretending otherwise would be worse than not shipping it.

The closest thing that is both real and reachable is SEBI's own feed, and it is
arguably more useful to a trader: it names the scrips under regulatory action.

```bash
cd "${CLAUDE_PROJECT_DIR}/.claude/skills/trader-sebi"

python sebi.py                                  # latest actions
python sebi.py --symbol MOKSH                   # anything naming a company
python sebi.py --kind manipulation --limit 20
python sebi.py --named-only --json
```

Exit 0 when entries matched, 1 when none did, 2 when the feed could not be read.

## What it reads

| source | status | what |
| --- | --- | --- |
| `sebi.gov.in/sebirss.xml` | **works** (200, ~14KB) | orders, enforcement, recoveries |
| `nseindia.com/api/corporates-pit` | unusable | returns `{"acqNameList":[],"data":[]}` — Akamai-gated |
| `api.bseindia.com/.../AnnGetData` | unusable | HTTP 403 to scripted clients |

Measured 2026-11-03. It writes nothing.

## Step 1: pick a class of action

| `--kind` | matches |
| --- | --- |
| `adjudication` | adjudication orders |
| `settlement` | settlement orders and applications |
| `recovery` | recovery certificates |
| `exemption` | exemption orders |
| `manipulation` | manipulation, front-running, insider-trading matters |

## Step 2: read the entity

Company names are parsed from SEBI's own phrasing ("in the matter of X Limited",
"in the scrip of Y"). When nothing can be parsed the entry still appears, marked
`(no entity parsed)`, rather than being dropped — a missed parse should not hide
an order.

## Step 3: treat it as a reason to read

An action naming a company is context, not a signal. Many concern conduct years
old, or an individual rather than the company.

## Rules

- Entity extraction is a text match on SEBI's phrasing and will miss unusual
  wordings. `--named-only` filters to successful parses; the default does not.
- Company names here are not NSE tickers. `--symbol` is a substring match on the
  headline text, so confirm the scrip before acting.
- It reports what the feed holds — roughly the latest 30 items, not an archive.

## What it never does

It never trades, never infers guilt, and never claims to have insider-trading
disclosures, which are not reachable from a script.

## Safety

- **This is not an insider-trade feed.** If you want Indian insider or promoter
  disclosures, they must be read from NSE's or BSE's website in a real browser;
  both block scripted access.
- A SEBI order is a legal document about past conduct. Do not read enforcement
  news as a directional view, and do not repeat allegations as fact.
- Nothing here is legal or investment advice.
