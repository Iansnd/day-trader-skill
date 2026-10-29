"""TypeSafe Jev (System 1) client shared by the trader tools. Standard library only.

One endpoint, three primitives (docs.typesafe.ai/primitives):
  noul    {"type": "noul",   "instructions": q, "criteria": {"true": .., "false": ..}}  -> noul 0..1
  choice  {"type": "choice", "instructions": q, "criteria": {name: description, ..}}    -> choice, confidence
  score   {"type": "score",  "instructions": q, "criteria": [level0, level1, ..]}       -> score, confidence
Questions in one call are evaluated in parallel, so ask several per call.

Off unless trader/config.json has {"jev": {"enabled": true}} and JEV_API_KEY is in the
environment or in trader/.env (launchd jobs have no shell environment).

Every tool imports this with:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))); import jev
"""

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

URL = "https://api.typesafe.ai/v1/systemone"
DEFAULTS = {
    "enabled": False,
    "model": "jev-latest",
    "timeout": 3,
    "workers": 16,
    "log": "trader/state/jev_scores.jsonl",
    "act_above": 0.6,
    "escalate_below": 0.4,  # ticket.py: confidence gate on p_works
    "rerank_keep": 20,
}  # scan.py: hits kept after Jev re-ranking
ERRORS = (urllib.error.URLError, OSError, KeyError, ValueError, TypeError)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def env_key(env_file="trader/.env"):
    key = os.environ.get("JEV_API_KEY")
    if key:
        return key
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(
                    r"^\s*(?:export\s+)?JEV_API_KEY\s*=\s*['\"]?([^'\"\s]+)", line
                )
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def settings(config_path="trader/config.json"):
    cfg = dict(DEFAULTS)
    try:
        with open(config_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        cfg.update(doc.get("jev") or {})
        cfg["market_regime"] = doc.get(
            "market_regime"
        )  # written by trader-scanner/regime.py
    except (OSError, ValueError):
        cfg["market_regime"] = None
    env_file = os.path.join(os.path.dirname(config_path) or ".", ".env")
    cfg["key"] = env_key(env_file)
    return cfg


def enabled(cfg):
    return bool(cfg.get("enabled") and cfg.get("key"))


def ask(cfg, state, questions, opener=None):
    """One call, many questions. Returns {"answers": {id: value}, "confidence": {id: c},
    "model", "latency_ms"}. value is the noul float, the choice name, or the score float.
    A question the server did not answer maps to None (so a partial mock still parses)."""
    if not isinstance(state, str):
        state = json.dumps(state, separators=(",", ":"), default=str)
    body = json.dumps(
        {"state": state, "model": cfg["model"], "questions": questions}
    ).encode("utf-8")
    req = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + cfg["key"],
            "Content-Type": "application/json",
            "User-Agent": "day-trader-skill/jev",
        },
    )
    t0 = time.time()
    with (opener or urllib.request.urlopen)(req, timeout=cfg["timeout"]) as r:
        doc = json.loads(r.read().decode("utf-8"))
    answers, conf = {}, {}
    for qid, q in questions.items():
        a = (doc.get("answers") or {}).get(qid)
        if a is None:
            answers[qid] = None
            continue
        if q["type"] == "noul":
            v = float(a["noul"])
            if not 0.0 <= v <= 1.0:
                raise ValueError("noul out of range: %r" % v)
            answers[qid] = round(v, 4)
        elif q["type"] == "choice":
            answers[qid] = str(a["choice"])
            conf[qid] = round(float(a.get("confidence", 0.0)), 4)
        else:
            answers[qid] = round(float(a["score"]), 4)
            conf[qid] = round(float(a.get("confidence", 0.0)), 4)
    return {
        "answers": answers,
        "confidence": conf,
        "model": doc.get("model", cfg["model"]),
        "latency_ms": int((time.time() - t0) * 1000),
    }


def ask_many(cfg, states, questions, opener=None):
    """ask() for each state concurrently. A failed call yields {"error": ...} in its slot."""

    def one(state):
        try:
            return ask(cfg, state, questions, opener)
        except ERRORS as e:
            return {"error": str(e)[:200]}

    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max(1, int(cfg.get("workers", 16)))) as ex:
        return list(ex.map(one, states))


def log(path, row):
    if not path:
        return
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def set_outcome(path, symbol, date, outcome):
    """Close the loop: fill outcome ("target", "stop" or "flat") on every logged score for
    symbol+date that has none. Idempotent, so a repeated alert changes nothing. Returns rows touched."""
    if (
        not path
        or not os.path.exists(path)
        or outcome not in ("target", "stop", "flat")
    ):
        return 0
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, n = [], 0
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if (
            row.get("symbol") == symbol
            and row.get("date") == date
            and row.get("outcome") is None
        ):
            row["outcome"] = outcome
            row["outcome_at"] = now_iso()
            n += 1
        out.append(json.dumps(row))
    if n:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        os.replace(tmp, path)
    return n


if __name__ == "__main__":
    import tempfile

    d = tempfile.mkdtemp()
    p = os.path.join(d, "j.jsonl")
    log(p, {"symbol": "TSLA", "date": "2026-10-28", "outcome": None, "p_works": 0.5})
    log(p, {"symbol": "TSLA", "date": "2026-10-27", "outcome": None, "p_works": 0.5})
    assert set_outcome(p, "TSLA", "2026-10-28", "target") == 1
    assert set_outcome(p, "TSLA", "2026-10-28", "stop") == 0, (
        "second write must not overwrite"
    )
    rows = [json.loads(l) for l in open(p)]
    assert rows[0]["outcome"] == "target" and rows[1]["outcome"] is None

    class R:
        def __init__(self, b):
            self.b = b

        def read(self):
            return self.b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    body = json.dumps(
        {
            "model": "jev-test",
            "answers": {
                "works": {"type": "noul", "noul": 0.7},
                "kind": {"type": "choice", "choice": "material", "confidence": 0.9},
                "hold": {"type": "score", "score": 2.5, "confidence": 0.4},
            },
        }
    ).encode()
    qs = {
        "works": {"type": "noul", "instructions": "?"},
        "kind": {"type": "choice", "instructions": "?", "criteria": {}},
        "hold": {"type": "score", "instructions": "?", "criteria": ["a", "b", "c"]},
        "missing": {"type": "noul", "instructions": "?"},
    }
    out = ask(
        {"model": "m", "timeout": 1, "key": "k"},
        {"x": 1},
        qs,
        opener=lambda req, timeout: R(body),
    )
    assert out["answers"] == {
        "works": 0.7,
        "kind": "material",
        "hold": 2.5,
        "missing": None,
    }, out
    assert out["confidence"] == {"kind": 0.9, "hold": 0.4}
    assert env_key(os.path.join(d, "none")) == os.environ.get("JEV_API_KEY")
    print("ok")
