"""
SEBI regulatory-action watch: which listed companies are named in SEBI orders.

The US original (trader-pelosi) reads congressional trade disclosures from the
House Clerk. India publishes no equivalent -- MPs' trades are not disclosed -- so
copying that skill literally is impossible, and pretending otherwise would be
worse than not shipping it.

The closest thing that is BOTH real and reachable is SEBI's own feed: adjudication
orders, settlement orders, front-running and market-manipulation cases, and
recovery certificates. That is more directly useful to a trader than politicians'
trades anyway -- it names the scrips under regulatory action.

Reachability was measured on 2026-11-03 before this was written:
    https://www.sebi.gov.in/sebirss.xml             200, ~14KB  <- used
    nseindia.com/api/corporates-pit?index=equities  200 but {"acqNameList":[],"data":[]}
    api.bseindia.com/.../AnnGetData                 403
So insider-trading disclosures from NSE/BSE are NOT available to this tool, and
it does not claim to have them.

    python sebi.py
    python sebi.py --symbol MOKSH
    python sebi.py --kind adjudication --limit 20 --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from data.sebi_feed import KINDS, SEBI_RSS, fetch_actions, filter_actions  # noqa: E402

# The feed parsing lives in data/sebi_feed.py so this skill and the Toolkit page
# classify and extract entities identically.
fetch = fetch_actions


def main() -> None:
    ap = argparse.ArgumentParser(description="SEBI regulatory actions naming listed companies.")
    ap.add_argument("--symbol", help="Filter to entries whose text contains this (company or scrip).")
    ap.add_argument("--kind", choices=sorted(KINDS), help="Narrow to one class of action.")
    ap.add_argument("--named-only", action="store_true",
                    help="Only entries where a company could be extracted.")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        items = fetch()
    except Exception as e:
        print(f"ERROR: could not read SEBI's feed ({type(e).__name__}: {e}).")
        sys.exit(2)

    rows = items
    if a.kind:
        rows = [x for x in rows if a.kind in x["kinds"]]
    if a.symbol:
        s = a.symbol.upper()
        rows = [x for x in rows if s in x["title"].upper()]
    if a.named_only:
        rows = [x for x in rows if x["entity"]]
    rows = rows[: a.limit]

    if a.json:
        print(json.dumps({"source": SEBI_RSS, "total_in_feed": len(items),
                          "returned": len(rows), "items": rows}, indent=2))
        sys.exit(0 if rows else 1)

    print(f"SEBI actions - {len(items)} in the feed, {len(rows)} shown"
          + (f", kind={a.kind}" if a.kind else "")
          + (f", matching {a.symbol}" if a.symbol else ""))
    if not rows:
        print("\nNothing matched.")
        sys.exit(1)
    for x in rows:
        tags = ",".join(x["kinds"]) or "-"
        who = x["entity"] or "(no entity parsed)"
        print(f"\n  [{tags}] {who}")
        print(f"    {x['title'][:150]}")
        if x["link"]:
            print(f"    {x['link']}")

    print("\nRegulatory actions, NOT insider-trade disclosures. India publishes no equivalent of")
    print("the US congressional feed, and NSE/BSE insider endpoints are not reachable from a")
    print("script (NSE returns an empty payload, BSE returns 403). An action naming a company is")
    print("a reason to read, not a trading signal, and often concerns past conduct.")
    sys.exit(0)


if __name__ == "__main__":
    main()
