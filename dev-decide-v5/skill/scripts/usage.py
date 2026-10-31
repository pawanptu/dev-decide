"""What Claude really ran after which kind of request, per project, so the routing hint can learn it.

When the hint server sees Claude run an agent or skill (PostToolUse) after a prompt in the same session, it keeps
one row: the prompt's meaning vector (256 numbers from the bundled meaning model, never the text), the agent or
skill, and the time. The next prompt that means much the same gets that agent or skill as its hint, ahead of the
generic models, when enough similar past prompts agree.

Rows live next to the decision graph's records, in ~/.claude/dev-decide/traces/usage/<project>.jsonl, never in the
repository; the newest MAX_ROWS per project are kept. "router_hook": {"learn": false} in .claude/dev-decide.json
turns it off.
"""
import json
import os
import threading
import time

import traces

MAX_ROWS = 400
# Chosen on the kit's labeled routing requests (leave-one-out, memories of 100 and 300 past uses): requests that
# need the same agent sit about 0.5-0.6 apart in meaning, and 0.55 with 3 agreeing neighbours answered 7.6% of
# requests (100 uses) to 18.2% (300 uses) at 87.5-100% right. Precision first: a wrong "learned" hint costs trust.
MIN_SIMILARITY = 0.55  # cosine of the meaning vectors: below this, a past prompt is about something else
MIN_VOTES = 3          # similar past prompts that must agree before the hint follows them
NEIGHBOURS = 7
_LOCK = threading.Lock()
_CACHE = {}  # file -> (mtime, rows)


def enabled(policy):
    return (policy.get("router_hook") or {}).get("learn", True) is not False and traces.enabled(policy)


def path(root, kind=""):
    return traces.folder() / "usage" / ("%s%s.jsonl" % (traces.slug(root), kind))


def record(root, vector, name, kind=""):
    """Remember that a prompt meaning `vector` led Claude to run `name` (kind ".files": to edit that file). Never raises."""
    try:
        p = path(root, kind)
        row = json.dumps({"ts": int(time.time() * 1000), "name": name, "v": [round(x, 4) for x in vector]},
                         separators=(",", ":")) + "\n"
        with _LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(str(p), "a", encoding="utf-8") as fh:
                fh.write(row)
            if p.stat().st_size > MAX_ROWS * 4000:  # about 1.5 times MAX_ROWS rows: keep the newest MAX_ROWS
                lines = p.read_text(encoding="utf-8").splitlines()[-MAX_ROWS:]
                tmp = p.with_name(p.name + ".tmp")
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(str(tmp), str(p))
    except Exception:
        pass


def _rows(root, kind=""):
    p = path(root, kind)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return []
    hit = _CACHE.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    rows = []
    with open(str(p), encoding="utf-8") as fh:
        for line in fh:
            try:
                r = json.loads(line)
                rows.append((r["name"], tuple(r["v"])))
            except (ValueError, KeyError, TypeError):
                pass
    rows = rows[-MAX_ROWS:]
    _CACHE[str(p)] = (mtime, rows)
    return rows


def suggest(root, vector, candidates):
    """(name, confidence, similar past prompts) when enough similar past prompts led Claude to the same agent or skill
    among `candidates`, else None. Confidence is the share of similar past prompts that agree, times how similar they
    are on average."""
    rows = _rows(root)
    if not rows:
        return None
    sims = []
    for name, v in rows:
        if name in candidates:
            s = sum(a * b for a, b in zip(vector, v))
            if s >= MIN_SIMILARITY:
                sims.append((s, name))
    if len(sims) < MIN_VOTES:
        return None
    sims.sort(reverse=True)
    near = sims[:NEIGHBOURS]
    votes = {}
    for s, name in near:
        votes.setdefault(name, []).append(s)
    name, ss = max(votes.items(), key=lambda kv: (len(kv[1]), sum(kv[1])))
    if len(ss) < MIN_VOTES:
        return None
    conf = len(ss) / float(len(near)) * (sum(ss) / len(ss))
    return name, round(conf, 3), len(near)


def suggest_files(root, vector, limit=3):
    """The files Claude edited after similar past prompts: up to `limit` (relative path, votes), when at least
    MIN_VOTES of the NEIGHBOURS most similar past edits (similarity >= MIN_SIMILARITY) name the same file."""
    sims = []
    for name, v in _rows(root, ".files"):
        s = sum(a * b for a, b in zip(vector, v))
        if s >= MIN_SIMILARITY:
            sims.append((s, name))
    sims.sort(reverse=True)
    votes = {}
    for s, name in sims[:NEIGHBOURS * 3]:
        votes[name] = votes.get(name, 0) + 1
    return sorted(((n, c) for n, c in votes.items() if c >= MIN_VOTES), key=lambda x: -x[1])[:limit]
