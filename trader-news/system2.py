#!/usr/bin/env python3
"""System 2 collector for trader-news. Standard library only.

The slow background layer. It reads ONLY the two agent-reach channels that
`agent-reach doctor` reports as working on this machine: RSS feeds and web
pages (Jina Reader, the same `r.jina.ai` route agent-reach's web channel uses).
Reddit, X, GitHub and the rest are deliberately not here; add a channel only
after doctor says "ok" for it.

    python3 system2.py                      # trader/system2_sources.json -> trader/system2.json
    python3 system2.py --sources my.json --out trader/system2.json --max-chars 4000

It collects and files. It does NOT touch trader/config.json. Claude reads
trader/system2.json, and if it wants a parameter change it writes a proposal
to trader/proposals.json; tightening may be applied, loosening waits for you.

trader/system2_sources.json:
  {"rss": ["https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
           "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US"],
   "web": ["https://www.federalreserve.gov/newsevents/pressreleases.htm"]}

Output trader/system2.json: {"collected_at", "rss": [{feed, title, link, published}],
"web": [{url, text}], "errors": [...]}. Items seen before are dropped via
trader/state/system2.json so each run shows only what is new.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

JINA = "https://r.jina.ai/"
UA = "trader-news/system2 (rss+web only)"


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text, feed):
    """RSS 2.0 or Atom -> [{feed, title, link, published}]."""
    if "<!DOCTYPE" in xml_text[:4096] or "<!ENTITY" in xml_text:
        raise ValueError("feed declares a DTD/entities; refused (XXE guard)")   # feeds never need one
    root = ET.fromstring(xml_text)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        row = {"feed": feed, "title": "", "link": "", "published": ""}
        for c in el:
            name = _local(c.tag)
            if name == "title":
                row["title"] = (c.text or "").strip()
            elif name == "link":
                row["link"] = (c.get("href") or c.text or "").strip()
            elif name in ("pubDate", "published", "updated"):
                row["published"] = row["published"] or (c.text or "").strip()
        if row["title"] or row["link"]:
            out.append(row)
    return out


def collect(sources, max_chars=4000, fetch=get):
    rss, web, errors = [], [], []
    for feed in sources.get("rss") or []:
        try:
            rss.extend(parse_feed(fetch(feed), feed))
        except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as e:
            errors.append({"source": feed, "error": str(e)[:200]})
    for url in sources.get("web") or []:
        try:
            web.append({"url": url, "text": fetch(JINA + url)[:max_chars]})
        except (urllib.error.URLError, OSError) as e:
            errors.append({"source": url, "error": str(e)[:200]})
    return rss, web, errors


def fp(item):
    return hashlib.sha1(
        (item.get("link") or item.get("url") or item.get("title") or "").encode()
    ).hexdigest()[:16]


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


def run(sources, state, max_chars=4000, fetch=get, now=None):
    """(doc, new_state): collect, drop what state has seen, remember the rest."""
    rss, web, errors = collect(sources, max_chars, fetch)
    seen = set(state.get("seen") or [])
    fresh_rss = [i for i in rss if fp(i) not in seen]
    now = now or datetime.datetime.now(datetime.timezone.utc)
    doc = {
        "created_by": "trader-news/system2",
        "channels": ["rss", "web"],
        "collected_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rss": fresh_rss,
        "web": web,
        "errors": errors,
        "note": "collect only; parameter changes go to trader/proposals.json, never config.json",
    }
    seen.update(fp(i) for i in rss)
    return doc, {"seen": sorted(seen)[-5000:]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="System 2 collector: RSS + web only.")
    ap.add_argument("--sources", default="trader/system2_sources.json")
    ap.add_argument("--out", default="trader/system2.json")
    ap.add_argument("--state", default="trader/state/system2.json")
    ap.add_argument("--max-chars", type=int, default=4000)
    a = ap.parse_args(argv)
    sources = load_json(a.sources, None)
    if sources is None:
        print(
            "system2.py: no %s (see the docstring for the shape)" % a.sources,
            file=sys.stderr,
        )
        return 1
    doc, state = run(sources, load_json(a.state, {}), a.max_chars)
    write_json(a.out, doc)
    write_json(a.state, state)
    print(
        "Wrote %s: %d new RSS items, %d pages, %d errors."
        % (a.out, len(doc["rss"]), len(doc["web"]), len(doc["errors"]))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
