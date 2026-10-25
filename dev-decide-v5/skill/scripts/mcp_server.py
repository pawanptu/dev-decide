#!/usr/bin/env python3
"""dev-decide as an MCP server (stdio), so any MCP client can use it: Cursor, Codex CLI, Gemini CLI, Windsurf,
Claude Desktop, Claude Code and others.

  python3 mcp_server.py [--cwd /path/to/repo]

Tools:
  decide    typed decisions about a request, issue, error or diff (a bundle such as pr or triage, or presets)
  correct   record the right answer for a past decision; dev-decide learns from it at once
  catalog   the bundles and presets it can answer, with their options

The repository is --cwd, else env DEV_DECIDE_CWD, else the directory the client starts the server in; a call may
name another one with "repo". Which model answers unsure questions follows your dev-decide settings (backend).
Inputs: plain text, an inline "diff", or DIFF, DIFF --staged and COMMIT <sha> of the repository. @path is refused:
an agent's tool call must not be able to read any file on this machine.
"""
import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ddcore as dd  # noqa: E402

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = ("dev-decide returns typed, calibrated decisions (a choice, a 1-5 score or a probability, each with "
                "a confidence) instead of prose. Call decide with a bundle such as pr, deploy, triage, incident or "
                "plan and the input; branch on the answers and gates (pass, review, block). A '?' or 'needs a "
                "human' means it is unsure: ask the user. Call catalog to see every question.")

TOOLS = [
    {
        "name": "decide",
        "title": "Decide",
        "description": ("Typed decisions about a request, issue, error message or code change. 'questions' names "
                        "bundles or presets, comma-separated: pr, deploy, triage, incident, plan, ai_change, "
                        "agent_release, ml_pr, ai_incident, or single presets such as risk, route, secret_leak, "
                        "breaking_change, task_type. Returns each answer with its confidence, and pass/review/block "
                        "gates for gated bundles."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "questions": {"type": "string", "description": "Bundles or presets, e.g. 'pr' or 'risk,breaking_change'."},
                "input": {"type": "string", "description": "The text to decide about, or DIFF, 'DIFF --staged' or 'COMMIT <sha>' to read the repository's changes."},
                "diff": {"type": "string", "description": "A unified diff sent inline (optional)."},
                "mode": {"type": "string", "enum": ["hybrid", "local", "llm"], "description": "hybrid (default): local first, a model only for what stays unsure. local: never call a model. llm: always ask the model."},
                "why": {"type": "boolean", "description": "Include the evidence behind each answer."},
                "repo": {"type": "string", "description": "Path of the repository, when it is not the server's own."},
            },
            "required": ["questions"],
        },
    },
    {
        "name": "correct",
        "title": "Correct a decision",
        "description": ("Record the right answer for a past decision, only when the user said it was wrong. "
                        "dev-decide learns from it at once for near-identical inputs."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answers": {"type": "object", "description": "The correct values, e.g. {\"issue_type\": \"bug\"}.",
                            "additionalProperties": {"type": ["string", "number", "boolean"]}},
                "id": {"type": "string", "description": "The decision id, or 'last' (default)."},
                "item": {"type": "string", "description": "The item id, for a batch decision."},
                "repo": {"type": "string", "description": "Path of the repository, when it is not the server's own."},
            },
            "required": ["answers"],
        },
    },
    {
        "name": "catalog",
        "title": "List questions",
        "description": "The bundles and presets dev-decide can answer, with the options of each choice.",
        "inputSchema": {"type": "object", "properties": {
            "repo": {"type": "string", "description": "Path of the repository, when it is not the server's own."}}},
    },
]


class ToolError(Exception):
    pass


def _repo(args, default):
    path = args.get("repo") or default
    p = Path(str(path)).expanduser()
    if not p.is_dir():
        raise ToolError("repo is not a directory: %s" % path)
    return str(p.resolve())


def _summary(out):
    return out.get("summary") or out.get("error") or ""


def tool_decide(args, cwd):
    import engine
    questions = str(args.get("questions") or "").strip()
    if not questions:
        raise ToolError("name at least one bundle or preset in 'questions'")
    text = str(args.get("input") or "")
    if text.lstrip().startswith("@"):
        raise ToolError("@path inputs are not read through MCP; send the text or a diff instead")
    spec = {"questions": questions, "input": text}
    if args.get("diff"):
        spec["diff"] = str(args["diff"])
    mode = str(args.get("mode") or "").lower()
    if mode in ("local", "llm"):
        spec["mode"] = mode
    if args.get("why"):
        spec["why"] = True
    return engine.decide(spec=spec, cwd=_repo(args, cwd))


def tool_correct(args, cwd):
    import learn
    answers = args.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise ToolError("'answers' must name at least one decision and its correct value")
    return learn.feedback(_repo(args, cwd), str(args.get("id") or "last"),
                          {str(k): v for k, v in answers.items()}, args.get("item"))


def tool_catalog(args, cwd):
    root = dd.project_root(_repo(args, cwd))
    cat = dd.load_catalog(dd.load_policy(root))
    out = {"bundles": {k: v for k, v in cat["bundles"].items()}, "choose": {}, "score": [], "verify": {}}
    for name, spec in cat["choose"].items():
        opts = spec.get("options") if isinstance(spec, dict) else None
        out["choose"][name] = opts if isinstance(opts, list) else "your agents and skills" if opts == "auto" else []
    out["score"] = sorted(cat["score"])
    for name, spec in cat["verify"].items():
        out["verify"][name] = spec if isinstance(spec, str) else (spec or {}).get("statement", "")
    out["gated_bundles"] = sorted(cat["gates"])
    return out


HANDLERS = {"decide": tool_decide, "correct": tool_correct, "catalog": tool_catalog}


def call_tool(name, args, cwd):
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": "unknown tool: %s" % name}], "isError": True}
    try:
        with contextlib.redirect_stdout(sys.stderr):  # stdout carries the protocol only
            out = handler(args if isinstance(args, dict) else {}, cwd)
    except ToolError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    except Exception as exc:
        return {"content": [{"type": "text", "text": "dev-decide failed: %s" % type(exc).__name__}], "isError": True}
    text = json.dumps(out, separators=(",", ":"))
    summary = _summary(out)
    return {"content": [{"type": "text", "text": (summary + "\n" + text) if summary else text}],
            "structuredContent": out, "isError": bool(out.get("error")) and not out.get("answers")}


def handle(msg, cwd):
    """One JSON-RPC message in, the response out (None for notifications)."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:  # a notification, such as notifications/initialized
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "dev-decide", "title": "dev-decide", "version": dd.VERSION},
                  "instructions": INSTRUCTIONS}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        result = call_tool(params.get("name"), params.get("arguments") or {}, cwd)
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cwd", default=os.environ.get("DEV_DECIDE_CWD") or os.getcwd())
    cwd = str(Path(ap.parse_args().cwd).expanduser().resolve())
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            if isinstance(msg, list):  # a batch (older protocol versions)
                reply = [r for r in (handle(m, cwd) for m in msg) if r is not None] or None
            else:
                reply = handle(msg, cwd)
        if reply is not None:
            out.write(json.dumps(reply, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()


if __name__ == "__main__":
    main()
