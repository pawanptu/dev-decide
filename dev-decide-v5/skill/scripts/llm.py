"""System 2: one lean model call for the questions the local stage could not settle.

Backends (policy "backend" or env DEV_DECIDE_BACKEND; default auto):
  api   Anthropic Messages API via ANTHROPIC_API_KEY, prompt-cached system prompt. Fastest.
  cli   `claude -p` with your Claude Code sign-in: Haiku, no tools, no settings, one turn.
  mock  canned answers for tests (DEV_DECIDE_MOCK=<file of decision lines>, or generated).
  none  no model; the engine falls back to local hints.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time

import ddcore as dd
import finalize
import inputs

SYSTEM_FILE = dd.SKILL_DIR / "prompts" / "system2.md"
BATCH_CHUNK = 8
MAX_PARALLEL = 4
MAX_INPUT_CHARS = 60000
CATEGORY_PRIORITY = ["migration", "code", "config", "infra", "ci", "prompt", "eval", "notebook", "manifest",
                     "test", "data", "docs", "lockfile", "style", "asset", "other"]
CHILD_ENV_DROP = ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_DIAGNOSTICS_FILE",
                  "CLAUDE_CODE_TEE_SDK_STDOUT")
RETRY_REMINDER = ("\n\nReminder: your reply must be decision lines only, exactly one line per listed key, "
                  "in the line format shown for each question.")


class LLMError(Exception):
    pass


def backend_choice(policy):
    forced = (os.environ.get("DEV_DECIDE_BACKEND") or str(policy.get("backend") or "auto")).lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_cli = bool(shutil.which("claude"))
    if forced == "mock" or forced == "none":
        return forced
    if forced == "api":
        return "api" if has_key else ("cli" if has_cli else "none")
    if forced == "cli":
        return "cli" if has_cli else ("api" if has_key else "none")
    return "api" if has_key else ("cli" if has_cli else "none")


def describe(q, routes):
    """One question with the exact line the model must write for it."""
    s = q.spec
    if q.kind == "choose":
        opts = s.get("options")
        if opts == "auto":
            opts = [r["name"] for r in routes]
        opts = list(opts or [])
        if s.get("allow_none") and "none" not in opts:
            opts.append("none")
        return "%s: choose one of %s\n  line: %s choose <option>=<p> <option>=<p> _rest=<p>" % (
            q.key, "|".join(opts), q.key)
    if q.kind == "score":
        lo, hi = int(s.get("min", 1)), int(s.get("max", 5))
        anchors = "; ".join("%s %s" % kv for kv in (s.get("anchors") or {}).items())
        label = s.get("label")
        levels = " ".join("%d=<p>" % i for i in range(lo, hi + 1))
        return "%s: score %d-%d%s%s\n  line: %s score %s" % (
            q.key, lo, hi, " (%s)" % label if label else "", ": " + anchors if anchors else "", q.key, levels)
    return "%s: verify: %s\n  line: %s verify <p_true>" % (q.key, s.get("statement", ""), q.key)


def _priority(f):
    cat = f.category or "other"
    rank = CATEGORY_PRIORITY.index(cat) if cat in CATEGORY_PRIORITY else len(CATEGORY_PRIORITY)
    return (rank, -len(f.added) - len(f.removed))


def _hint_line(key, ans):
    if ans.kind == "verify":
        val = "p_true about %.2f" % ans.p
    else:
        top = sorted(((k, v) for k, v in ans.probs.items() if k != "_rest"), key=lambda kv: -kv[1])[:2]
        val = ", ".join("%s %.2f" % kv for kv in top)
    why = ("; " + "; ".join(ans.evidence[:2])) if ans.evidence else ""
    return "- [hint] local estimate for %s: %s%s" % (key, val, why)


def build_messages(chunk, questions, ctx, flags, batch):
    system = SYSTEM_FILE.read_text(encoding="utf-8")
    keys = []
    for st in chunk:
        for k in st.pending:
            if k not in keys:
                keys.append(k)
    lines = ["# Questions"]
    lines += ["- " + describe(questions[k], ctx["routes"]) for k in keys]
    lines.append("")
    lines.append("Evidence requested: add one short why line per key." if "why" in flags else
                 "Evidence not requested: do not write why lines.")
    if "deep" in flags:
        lines.append("Deep mode: before answering, use the Read, Grep and Glob tools to check the repository "
                     "wherever the answer depends on code that is not in the input (callers, tests, configuration, "
                     "related modules). Cite what you found in why lines. Your final message is the decision lines only.")
    policy = ctx["policy"]
    notes = [str(n) for n in (policy.get("notes") or [])][:10]
    if notes:
        lines += ["", "# Project notes"] + ["- " + n for n in notes]
    precedents = ctx.get("precedents") or []
    if precedents:  # corrections the user made to earlier decisions on similar inputs
        lines += ["", "# The user's corrections of earlier, similar decisions",
                  "Follow these conventions when an input is the same kind of case:"] + ["- " + x for x in precedents[:6]]
    if any(questions[k].preset == "route" or questions[k].spec.get("options") == "auto" for k in keys) and ctx["routes"]:
        lines += ["", "# Route options"] + ["- %s [%s]: %s" % (r["name"], r["kind"], r["description"])
                                             for r in ctx["routes"]]
    budget = max(4000, MAX_INPUT_CHARS // max(1, len(chunk)))
    for n, st in enumerate(chunk, 1):
        lines.append("")
        if batch:
            lines.append("# Item %d" % n)
        lines.append("Answer these keys%s: %s" % (" for this item" if batch else "", ", ".join(st.pending)))
        facts = st.facts.facts[:14]
        hints = [_hint_line(k, st.hints[k]) for k in st.pending if k in st.hints]
        if facts or hints:
            lines.append("## Local findings")
            lines += ["- [%s] %s" % ("fact" if s == "exact" else "hint", t) for s, t in facts]
            lines += hints[:12]
        lines.append("## Input")
        lines.append(inputs.llm_view(st.item, max_chars=budget, priority=_priority))
    lines.append("")
    if batch:
        lines.append("Write the decision lines now: an item line for each item, then one line for each of its listed keys.")
    else:
        lines.append("Write the decision lines now: exactly one line for each of these keys, in this order: %s."
                     % ", ".join(keys))
    return system, "\n".join(lines)


def _call_cli(system, user, model, root, deep, timeout, effort=None):
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("the claude command was not found")
    fd, sysfile = tempfile.mkstemp(prefix="dd-system-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system)
        args = [exe, "-p", "--model", model, "--system-prompt-file", sysfile,
                "--max-turns", "8" if deep else "1", "--setting-sources", "", "--strict-mcp-config",
                "--no-session-persistence", "--output-format", "json"]
        if effort:
            args += ["--effort", effort]
        if deep:
            args += ["--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob"]
        else:
            args += ["--tools", ""]
        env = dict(os.environ)
        env["DEV_DECIDE_CHILD"] = "1"
        for var in CHILD_ENV_DROP:  # keep the nested call away from the parent session's channels
            env.pop(var, None)
        try:
            proc = subprocess.run(args, input=user, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, cwd=str(root), env=env)
        except subprocess.TimeoutExpired:
            raise LLMError("model call timed out after %ds" % timeout)
    finally:
        try:
            os.unlink(sysfile)
        except OSError:
            pass
    out = (proc.stdout or "").strip()
    if os.environ.get("DEV_DECIDE_DEBUG"):
        try:
            (dd.state_dir(root) / "last_model_call.json").write_text(
                json.dumps({"args": args[1:], "user": user, "stdout": out, "stderr": proc.stderr}, indent=1),
                encoding="utf-8")
        except Exception:
            pass
    info = {}
    try:
        data = json.loads(out)
        text = data.get("result") or ""
        info = {"cost_usd": data.get("total_cost_usd"), "api_ms": data.get("duration_api_ms"),
                "turns": data.get("num_turns")}
        if data.get("is_error") and not text:
            raise LLMError(str(data.get("subtype") or "model call failed"))
    except ValueError:
        text = out
    if proc.returncode != 0 and not text:
        raise LLMError((proc.stderr or "").strip()[:300] or "claude exited with code %d" % proc.returncode)
    return text, info


def _call_api(system, user, model, timeout):
    import urllib.error
    import urllib.request
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    url = os.environ.get("DEV_DECIDE_API_URL") or "https://api.anthropic.com/v1/messages"
    body = {"model": model, "max_tokens": 2048, "temperature": 0,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}]}
    data = None
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code in (429, 500, 502, 503, 529) and attempt == 0:
                time.sleep(1.0)
                continue
            raise LLMError("API error %d: %s" % (exc.code, detail))
        except urllib.error.URLError as exc:
            raise LLMError("API unreachable: %s" % exc.reason)
        except Exception as exc:
            raise LLMError("API call failed: %s" % type(exc).__name__)
    text = "".join(b.get("text", "") for b in (data or {}).get("content", []) if b.get("type") == "text")
    usage = (data or {}).get("usage") or {}
    return text, {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                  "cache_read_tokens": usage.get("cache_read_input_tokens")}


def _oracle():
    """Tests only: DEV_DECIDE_MOCK_ORACLE names a JSON file {normalized text: {key: label}}."""
    path = os.environ.get("DEV_DECIDE_MOCK_ORACLE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _mock_key(text):
    return " ".join((text or "").lower().split())


def _mock_lines(chunk, questions, batch, strong=False):
    path = os.environ.get("DEV_DECIDE_MOCK")
    if path:
        with open(path, encoding="utf-8") as fh:
            return fh.read(), {"mock": path}
    oracle = _oracle()
    out = []
    for n, st in enumerate(chunk, 1):
        if batch:
            out.append("item %d" % n)
        known = (oracle or {}).get(_mock_key(st.item.text)) or {}
        if not strong and os.environ.get("DEV_DECIDE_MOCK_WEAK"):  # tests: the fast model is unsure
            known = {}
        for k in st.pending:
            q = questions[k]
            if known.get(k) is not None:
                if q.kind == "verify":
                    out.append("%s verify %s" % (k, "0.9" if str(known[k]).lower() == "true" else "0.1"))
                elif q.kind == "score":
                    out.append("%s score %s=0.9" % (k, known[k]))
                else:
                    out.append("%s choose %s=0.9 _rest=0.1" % (k, known[k]))
                continue
            top = 0.4 if os.environ.get("DEV_DECIDE_MOCK_WEAK") else 0.6
            if q.kind == "verify":
                out.append("%s verify 0.5" % k)
            elif q.kind == "score":
                lo, hi = int(q.spec.get("min", 1)), int(q.spec.get("max", 5))
                mid = (lo + hi) // 2
                out.append("%s score %d=%.1f %d=%.1f %d=%.1f" % (k, mid, top, max(lo, mid - 1), (1 - top) / 2,
                                                               min(hi, mid + 1), (1 - top) / 2))
            else:
                opts = q.spec.get("options")
                opts = [r["name"] for r in []] if opts == "auto" else list(opts or [])
                first = opts[0] if opts else "none"
                out.append("%s choose %s=%.1f _rest=%.1f" % (k, first, top, 1 - top))
    return "\n".join(out), {"mock": "generated"}


def call_model(system, user, policy, root, deep=False, timeout=None):
    backend = backend_choice(policy)
    forced = policy.get("_force_model") or {}  # escalation to a stronger model (engine._escalate)
    if backend == "api":
        model = forced.get("api") or dd.model_for(policy, "api")
        text, info = _call_api(system, user, model, timeout or 60)
    elif backend == "cli":
        model = forced.get("cli") or dd.model_for(policy, "cli")
        effort = os.environ.get("DEV_DECIDE_EFFORT", policy.get("effort", "low"))
        text, info = _call_cli(system, user, model, root, deep, timeout or (150 if deep else 60),
                               effort=None if str(effort).lower() in ("", "default", "none") else effort)
    else:
        raise LLMError("no model backend available")
    info["backend"] = "%s:%s" % (backend, model)
    return text, info


def ask(work, questions, ctx, flags, policy, root):
    """Ask the model about every pending key of every item in `work`.

    Returns ({item_index: {key: raw_answer}}, info). Raises nothing; failures come back in info["error"].
    """
    backend = backend_choice(policy)
    deep = "deep" in flags
    batch = len(work) > 1 or (work and work[0].item.id is not None)
    chunks = [work[i:i + BATCH_CHUNK] for i in range(0, len(work), BATCH_CHUNK)]
    via = "llm-deep" if deep and backend == "cli" else "llm"

    def readable(parsed):
        if "items" in parsed:
            return sum(len(it.get("answers", {})) for it in parsed["items"])
        return len(parsed.get("answers", {}))

    def run_chunk(chunk):
        system, user = build_messages(chunk, questions, ctx, flags, batch)
        started = time.perf_counter()
        known = set(k for st in chunk for k in st.pending)
        kinds = {k: questions[k].kind for k in known}
        if backend == "mock":
            text, info = _mock_lines(chunk, questions, batch, strong=bool(policy.get("_force_model")))
            info["backend"] = "mock-strong" if policy.get("_force_model") else "mock"
            parsed = finalize.parse_lines(text, known_keys=known, strict=False, kinds=kinds)
        else:
            text, info = call_model(system, user, policy, root, deep=deep)
            parsed = finalize.parse_lines(text, known_keys=known, strict=False, kinds=kinds)
            if not readable(parsed):  # rare: the model ignored the format; ask once more
                text, info2 = call_model(system, user + RETRY_REMINDER, policy, root, deep=deep)
                parsed = finalize.parse_lines(text, known_keys=known, strict=False, kinds=kinds)
                if info.get("cost_usd") is not None and info2.get("cost_usd") is not None:
                    info["cost_usd"] = info["cost_usd"] + info2["cost_usd"]
                info["retried"] = True
        info["ms"] = int((time.perf_counter() - started) * 1000)
        mapping = {}
        if "items" in parsed:
            for it in parsed["items"]:
                try:
                    idx = int(it["id"]) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(chunk):
                    mapping[chunk[idx].index] = it.get("answers", {})
        else:
            mapping[chunk[0].index] = parsed.get("answers", {})
        for st in chunk:
            got = mapping.get(st.index, {})
            for k, a in list(got.items()):
                if k not in st.pending:
                    del got[k]
                    continue
                a["via"] = via
                q = questions[k]
                if q.preset:
                    a["preset"] = q.preset
                else:
                    a["type"] = q.kind
                    for f in ("options", "allow_none", "min", "max", "statement"):
                        if f in q.spec:
                            a[f] = q.spec[f]
                if q.spec.get("options") == "auto" and not a.get("options"):
                    a["options"] = [r["name"] for r in ctx["routes"]]
            mapping[st.index] = got
        return mapping, info

    results, infos, errors = {}, [], []
    if backend == "none":
        return results, {"backend": "none", "error": "no model backend available (install Claude Code or set ANTHROPIC_API_KEY)"}
    from concurrent.futures import ThreadPoolExecutor
    workers = min(MAX_PARALLEL, len(chunks)) or 1
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(run_chunk, c) for c in chunks]
        for fut in futures:
            try:
                mapping, info = fut.result()
                results.update(mapping)
                infos.append(info)
            except LLMError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append("model call failed: %s" % type(exc).__name__)
    info = {"backend": infos[0].get("backend") if infos else backend,
            "calls": len(chunks) + sum(1 for i in infos if i.get("retried")),
            "ms": max([i.get("ms", 0) for i in infos] or [0])}
    cost = [i.get("cost_usd") for i in infos if i.get("cost_usd") is not None]
    if cost:
        info["cost_usd"] = round(sum(cost), 5)
    if errors:
        info["error"] = errors[0]
    return results, info
