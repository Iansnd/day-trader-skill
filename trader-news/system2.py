#!/usr/bin/env python3
"""System 2 collector for trader-news. Standard library only.

The slow background layer. It reads RSS feeds, web pages (Jina Reader, the
same `r.jina.ai` route agent-reach's web channel uses) and, when XAI_API_KEY is
set, X posts through xAI's x_search tool: one search per watchlist symbol, capped
per run. Reddit, GitHub and the rest are deliberately not here.

    python3 system2.py                      # trader/system2_sources.json -> trader/system2.json
    python3 system2.py --sources my.json --out trader/system2.json --max-chars 4000

It collects and files. It does NOT touch trader/config.json. Claude reads
trader/system2.json, and if it wants a parameter change it writes a proposal
to trader/proposals.json; tightening may be applied, loosening waits for you.

trader/system2_sources.json:
  {"rss": ["https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&output=atom",
           "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY&region=US&lang=en-US"],
   "web": ["https://www.federalreserve.gov/newsevents/pressreleases.htm"],
   "x": {"max_searches": 8, "watchlist": "trader/watchlist.json", "symbols": ["SPY"]}}

The x channel needs XAI_API_KEY in the environment or in trader/.env. It searches
the symbols listed, then the first watchlist hits, until max_searches is reached
(each search is one billed x_search call; keep the cap small).

Output trader/system2.json: {"collected_at", "rss": [{feed, title, link, published}],
"web": [{url, text}], "x": [{symbol, text, url, author, date, likes}],
"x_usage": {searches, tokens}, "errors": [...]}. Items seen before are dropped via
trader/state/system2.json so each run shows only what is new.
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

JINA = "https://r.jina.ai/"
UA = "trader-news/system2"
XAI_URL = "https://api.x.ai/v1/responses"
XAI_MODEL = "grok-4-1-fast"
X_PROMPT = (
    "Search X for posts from {frm} to {to} about the stock ${sym}: news, earnings, "
    "guidance, analyst calls, unusual moves, rumours. Return ONLY JSON: "
    '{{"items": [{{"text": "...", "url": "https://x.com/...", "author": "handle", '
    '"date": "YYYY-MM-DD", "likes": 0}}]}}. At most {n} posts, most relevant first; '
    'empty "items" if nothing substantive.'
)


def user_agent(config="trader/config.json"):
    """SEC refuses generic agents: use sec_user_agent from the config when set."""
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        try:
            with open(config, encoding="utf-8") as fh:
                ua = str(json.load(fh).get("sec_user_agent") or "").strip()
        except (OSError, ValueError):
            ua = ""
    return ua or UA


def get(url, timeout=20):
    # The SEC agent carries a name and email: only sec.gov gets it, everyone else sees the generic UA.
    host = urllib.parse.urlsplit(url).hostname or ""
    ua = user_agent() if host == "sec.gov" or host.endswith(".sec.gov") else UA
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
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


def xai_key(env_file="trader/.env"):
    """XAI_API_KEY from the environment, else from trader/.env (export KEY=v or KEY=v)."""
    key = os.environ.get("XAI_API_KEY", "").strip()
    if key:
        return key
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].strip()
                if line.startswith("XAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def x_symbols(xcfg, max_searches):
    """Configured symbols first, then watchlist hits, deduped, capped."""
    syms = list(xcfg.get("symbols") or [])
    wl = load_json(xcfg.get("watchlist", "trader/watchlist.json"), {})
    syms += [h.get("symbol") for h in wl.get("hits") or [] if h.get("symbol")]
    out = []
    for s in syms:
        if s and s not in out:
            out.append(s)
    return out[:max_searches]


def x_search(sym, key, now, per_symbol=5, timeout=60, post=None):
    """One billed x_search call -> (items, tokens_used)."""
    to = now.strftime("%Y-%m-%d")
    frm = (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    payload = {
        "model": XAI_MODEL,
        "tools": [{"type": "x_search", "from_date": frm, "to_date": to}],
        "input": [{"role": "user", "content": X_PROMPT.format(frm=frm, to=to, sym=sym, n=per_symbol)}],
    }
    if post is None:
        req = urllib.request.Request(
            XAI_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    else:
        resp = post(payload)
    return parse_x(resp, sym), (resp.get("usage") or {}).get("total_tokens", 0)


def parse_x(resp, sym):
    """xAI responses JSON -> [{symbol, text, url, author, date, likes}]."""
    if resp.get("error"):
        raise ValueError(str(resp["error"])[:200])
    text = ""
    for o in resp.get("output") or []:
        if isinstance(o, dict) and o.get("type") == "message":
            for c in o.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "output_text":
                    text = c.get("text") or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON in x_search output: %r" % text[:120])
    items = json.loads(text[start:end + 1]).get("items") or []
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("url"):
            out.append({
                "symbol": sym,
                "text": str(it.get("text") or "")[:500],
                "url": it["url"],
                "author": str(it.get("author") or "").lstrip("@"),
                "date": it.get("date"),
                "likes": it.get("likes"),
            })
    return out


def collect_x(xcfg, now, post=None, key=None):
    """(items, usage, errors). No key or no config -> nothing, silently."""
    if not xcfg:
        return [], {"searches": 0, "tokens": 0}, []
    key = key or xai_key()
    if not key:
        return [], {"searches": 0, "tokens": 0}, [{"source": "x", "error": "XAI_API_KEY not set"}]
    items, errors, searches, tokens = [], [], 0, 0
    for sym in x_symbols(xcfg, int(xcfg.get("max_searches", 8))):
        searches += 1
        try:
            got, used = x_search(sym, key, now, int(xcfg.get("per_symbol", 5)), post=post)
            items.extend(got)
            tokens += used
        except (urllib.error.URLError, OSError, ValueError) as e:
            errors.append({"source": "x:" + sym, "error": str(e)[:200]})
    return items, {"searches": searches, "tokens": tokens}, errors


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


JEV_TRIAGE = {
    "kind": {"type": "choice",
             "instructions": "For an intraday long-only US stock trader, what is this news item?",
             "criteria": {"material": "moves a stock or the market today: earnings, guidance, Fed, macro print, "
                                      "deal, halt, major product or legal news",
                          "noise": "opinion, recap, promotion, or old news with no new fact",
                          "risk": "a hazard to open longs: downgrade, probe, outage, guidance cut, tariff, war"}},
    "thesis_change": {"type": "noul", "instructions": "Would this item change a trader's thesis on the stock or "
                                                      "the market for today's session?"},
}


def triage(items, jev_cfg):
    """Jev tags each fresh item {kind, confidence, p_thesis_change} in place; off -> untouched."""
    if not items or not jev_cfg:
        return items
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import jev
    if not jev.enabled(jev_cfg):
        return items
    states = [{k: it.get(k) for k in ("feed", "title", "symbol", "text", "author", "date") if it.get(k)} for it in items]
    for it, g in zip(items, jev.ask_many(jev_cfg, states, JEV_TRIAGE)):
        a = g.get("answers") or {}
        it["jev"] = ({"kind": a.get("kind"), "confidence": (g.get("confidence") or {}).get("kind"),
                      "p_thesis_change": a.get("thesis_change")} if "answers" in g else {"error": g.get("error")})
    return items


def run(sources, state, max_chars=4000, fetch=get, now=None, post=None, jev_cfg=None):
    """(doc, new_state): collect, drop what state has seen, remember the rest."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    rss, web, errors = collect(sources, max_chars, fetch)
    x, x_usage, x_errors = collect_x(sources.get("x"), now, post=post)
    seen = set(state.get("seen") or [])
    fresh_rss = [i for i in rss if fp(i) not in seen]
    fresh_x = [i for i in x if fp(i) not in seen]
    triage(fresh_rss + fresh_x, jev_cfg)
    doc = {
        "created_by": "trader-news/system2",
        "channels": ["rss", "web"] + (["x"] if sources.get("x") else []),
        "collected_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rss": fresh_rss,
        "web": web,
        "x": fresh_x,
        "x_usage": x_usage,
        "errors": errors + x_errors,
        "note": "collect only; parameter changes go to trader/proposals.json, never config.json",
    }
    seen.update(fp(i) for i in rss + x)
    return doc, {"seen": sorted(seen)[-5000:]}


def market_hours(now=None):
    """True Mon-Fri 09:15-16:15 New York; DST-safe, so a fixed timer can run all day."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from news import to_eastern   # same DST rule as the rest of trader-news; 3.8-safe
    t = to_eastern(now or datetime.datetime.now(datetime.timezone.utc))
    return t.weekday() < 5 and (9, 15) <= (t.hour, t.minute) < (16, 15)


def main(argv=None):
    ap = argparse.ArgumentParser(description="System 2 collector: RSS + web + X.")
    ap.add_argument("--sources", default="trader/system2_sources.json")
    ap.add_argument("--out", default="trader/system2.json")
    ap.add_argument("--state", default="trader/state/system2.json")
    ap.add_argument("--max-chars", type=int, default=4000)
    ap.add_argument("--config", default="trader/config.json")
    ap.add_argument("--market-hours-only", action="store_true",
                    help="exit 0 without collecting outside 09:15-16:15 New York, Mon-Fri")
    a = ap.parse_args(argv)
    if a.market_hours_only and not market_hours():
        return 0
    sources = load_json(a.sources, None)
    if sources is None:
        print(
            "system2.py: no %s (see the docstring for the shape)" % a.sources,
            file=sys.stderr,
        )
        return 1
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import jev
    doc, state = run(sources, load_json(a.state, {}), a.max_chars, jev_cfg=jev.settings(a.config))
    write_json(a.out, doc)
    write_json(a.state, state)
    print(
        "Wrote %s: %d new RSS items, %d pages, %d new X posts (%d searches, %d tokens), %d errors."
        % (a.out, len(doc["rss"]), len(doc["web"]), len(doc["x"]),
           doc["x_usage"]["searches"], doc["x_usage"]["tokens"], len(doc["errors"]))
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
