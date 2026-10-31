"""Token use of Claude's replies, counted from the numbers Claude Code already writes in its transcript.

Each assistant message in a transcript carries the model's own token counts ("usage"). A message is written as
several transcript lines (one per content block) that repeat the same counts, so they are counted once per message.
Only the four counts are kept (never any text), so the decision graph can show what a prompt cost and the numbers
can be compared with and without dev-decide's guard.

  input        tokens read fresh (not from the cache)
  cache_write  tokens written to the prompt cache
  cache_read   tokens read back from the cache (the cheap ones)
  output       tokens the model wrote (replies, thinking and tool calls)
  calls        model calls (one per assistant message)
"""

FIELDS = (("input", "input_tokens"), ("cache_write", "cache_creation_input_tokens"),
          ("cache_read", "cache_read_input_tokens"), ("output", "output_tokens"))


def count(rows):
    """Totals over the assistant messages in `rows` (parsed transcript lines), or None when there are none."""
    seen = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("type") != "assistant":
            continue
        m = r.get("message") if isinstance(r.get("message"), dict) else {}
        u = m.get("usage")
        if not isinstance(u, dict) or m.get("model") == "<synthetic>":
            continue
        key = m.get("id") or r.get("requestId") or r.get("uuid")
        if key is None:
            continue
        cur = seen.setdefault(key, {})
        for name, field in FIELDS:  # lines of one message repeat its counts; a later line may hold the final output
            v = u.get(field)
            if isinstance(v, (int, float)) and v > cur.get(name, 0):
                cur[name] = int(v)
    if not seen:
        return None
    out = {name: sum(c.get(name, 0) for c in seen.values()) for name, _ in FIELDS}
    out["calls"] = len(seen)
    return out


def add(a, b):
    """The sum of two counts (either may be None)."""
    if not a:
        return dict(b) if b else None
    if not b:
        return dict(a)
    return {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in ("input", "cache_write", "cache_read", "output", "calls")}


def fresh(t):
    """The tokens that were not read from the cache: what a reply really added (input + cache writes + output)."""
    return int(t.get("input", 0)) + int(t.get("cache_write", 0)) + int(t.get("output", 0)) if t else 0


def total(t):
    return fresh(t) + int(t.get("cache_read", 0)) if t else 0


def short(n):
    """12345 -> '12.3k', 1234567 -> '1.23M'."""
    n = int(n or 0)
    if n >= 1000000:
        return "%.2fM" % (n / 1000000.0)
    if n >= 10000:
        return "%dk" % round(n / 1000.0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)
