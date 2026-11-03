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

SEBI_RSS = "https://www.sebi.gov.in/sebirss.xml"

KINDS = {
    "adjudication": ("adjudication order",),
    "settlement": ("settlement order", "settlement application"),
    "recovery": ("recovery certificate",),
    "exemption": ("exemption order",),
    "manipulation": ("manipulation", "front running", "front-running", "insider trading"),
}

# "in the matter of X Limited" / "in the scrip of Y Ltd" -- SEBI's own phrasing.
_ENTITY = re.compile(
    r"in the (?:matter|scrip) of\s+(.+?)(?:\s*[.,]|\s+\(|$)", re.IGNORECASE)


def fetch(timeout: int = 20) -> list[dict]:
    req = urllib.request.Request(SEBI_RSS, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        low = title.lower()
        kinds = [k for k, words in KINDS.items() if any(w in low for w in words)]
        m = _ENTITY.search(title)
        # SEBI's feed sometimes emits an absolute URL already prefixed with its
        # own base, giving "https://www.sebi.gov.in/https://www.sebi.gov.in/...".
        # Keep the last absolute URL in the string so the link is usable.
        link = (it.findtext("link") or "").strip()
        if link.count("http") > 1:
            link = "http" + link.rsplit("http", 1)[1]

        items.append({
            "title": title,
            "link": link,
            "published": (it.findtext("pubDate") or "").strip(),
            "kinds": kinds,
            "entity": m.group(1).strip() if m else None,
        })
    return items


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
