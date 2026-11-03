"""
SEBI regulatory actions naming listed Indian companies.

Shared by .claude/skills/trader-sebi and backend/skills_api.py.

This is the Indian stand-in for a US congressional-disclosure feed, which has no
equivalent here -- MPs' trades are not disclosed. Reachability was measured on
2026-11-03 before this was written:

    https://www.sebi.gov.in/sebirss.xml              200, ~14KB   -> used
    nseindia.com/api/corporates-pit?index=equities   200 but {"acqNameList":[],"data":[]}
    api.bseindia.com/.../AnnGetData                  403

So insider/promoter disclosures are NOT available to this platform from a
script, and nothing here claims to have them. What SEBI's own feed does give is
arguably more useful to a trader: the scrips under regulatory action.
"""
from __future__ import annotations

import re
import urllib.request
import xml.etree.ElementTree as ET

SEBI_RSS = "https://www.sebi.gov.in/sebirss.xml"

KINDS = {
    "adjudication": ("adjudication order",),
    "settlement": ("settlement order", "settlement application"),
    "recovery": ("recovery certificate",),
    "exemption": ("exemption order",),
    "manipulation": ("manipulation", "front running", "front-running", "insider trading"),
}

# SEBI's own phrasing: "in the matter of X Limited", "in the scrip of Y".
_ENTITY = re.compile(r"in the (?:matter|scrip) of\s+(.+?)(?:\s*[.,]|\s+\(|$)", re.IGNORECASE)


def fetch_actions(timeout: int = 20) -> list[dict]:
    req = urllib.request.Request(SEBI_RSS, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        root = ET.fromstring(r.read())

    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        # SEBI sometimes emits a link already prefixed with its own base, giving
        # "https://www.sebi.gov.in/https://www.sebi.gov.in/...". Keep the last
        # absolute URL so the link is usable.
        link = (it.findtext("link") or "").strip()
        if link.count("http") > 1:
            link = "http" + link.rsplit("http", 1)[1]

        low = title.lower()
        m = _ENTITY.search(title)
        items.append({
            "title": title,
            "link": link,
            "published": (it.findtext("pubDate") or "").strip(),
            "kinds": [k for k, words in KINDS.items() if any(w in low for w in words)],
            "entity": m.group(1).strip() if m else None,
        })
    return items


def filter_actions(items: list[dict], kind: str | None = None, query: str | None = None,
                   named_only: bool = False, limit: int = 15) -> list[dict]:
    rows = items
    if kind:
        rows = [x for x in rows if kind in x["kinds"]]
    if query:
        q = query.upper()
        rows = [x for x in rows if q in x["title"].upper()]
    if named_only:
        rows = [x for x in rows if x["entity"]]
    return rows[:limit]
