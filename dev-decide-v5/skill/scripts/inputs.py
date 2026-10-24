"""Resolve request inputs and parse unified diffs.

Input references inside the text after "::":
  DIFF            uncommitted changes vs HEAD, plus untracked files
  DIFF --staged   staged changes only
  COMMIT <sha>    one commit (message and patch)
  @path           a file (several are allowed)
Anything else is free text (an issue, a log, a request, a stack trace).
A JSON request can also carry a unified diff inline ("diff"), which is parsed like DIFF.
"""
import re
from pathlib import Path

import ddcore as dd

# Whether DIFF, COMMIT and @path read from this machine. serve.py turns this off when it listens beyond
# localhost, so remote callers cannot read the server's files; they send diffs inline instead.
REFS = True

MAX_FILE_BYTES = 2000000
MAX_CURRENT_BYTES = 300000
MAX_UNTRACKED = 50
MAX_UNTRACKED_BYTES = 200000
REF = re.compile(r"(?<!\S)(DIFF(?:\s+--staged)?|COMMIT\s+([0-9A-Za-z_./~^@{}-]{1,80})|@([^\s]+))(?!\S)")
SAFE_REV = re.compile(r"^[0-9A-Za-z_./~^@{}][0-9A-Za-z_./~^@{}-]{0,79}$")  # no leading "-": it would be a git option


class FileChange(object):
    __slots__ = ("path", "old_path", "status", "added", "added_ln", "removed", "binary", "category")

    def __init__(self, path):
        self.path = path
        self.old_path = None
        self.status = "modified"
        self.added = []
        self.added_ln = []
        self.removed = []
        self.binary = False
        self.category = None


class Item(object):
    def __init__(self, item_id):
        self.id = item_id
        self.kind = "empty"        # diff | file | text | mixed | empty
        self.text = ""             # free text part
        self.files = []            # FileChange list (diff hunks, or whole files for @path)
        self.diff_text = ""        # raw diff for the model
        self.commit_message = ""
        self.refs = []
        self.error = None
        self.notes = []
        self._current = {}
        self.root = None

    def current(self, path):
        """Current on-disk content of a changed file (for whole-file checks), cached."""
        if path in self._current:
            return self._current[path]
        content = ""
        if self.root is not None:
            p = Path(self.root) / path
            try:
                if p.is_file() and p.stat().st_size <= MAX_CURRENT_BYTES:
                    content = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
        self._current[path] = content
        return content

    def fingerprint(self):
        parts = [self.kind, self.text, self.diff_text, self.commit_message]
        for f in self.files:
            if self.kind == "file":
                parts.append(f.path)
                parts.append("\n".join(f.added))
        return dd.stable_hash(*parts)


def parse_diff(text):
    files, cur, in_hunk, new_ln = [], None, False, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
            cur = FileChange(m.group(2) if m else line[11:].strip())
            files.append(cur)
            in_hunk = False
            continue
        if cur is None:
            continue
        if not in_hunk:
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path, cur.status = line[12:].strip(), "renamed"
            elif line.startswith("rename to "):
                cur.path = line[10:].strip()
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                cur.binary = True
            elif line.startswith("@@"):
                m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
                new_ln, in_hunk = (int(m.group(1)) if m else 1), True
            continue
        if line.startswith("@@"):
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
            new_ln = int(m.group(1)) if m else new_ln
        elif line.startswith("+"):
            cur.added.append(line[1:])
            cur.added_ln.append(new_ln)
            new_ln += 1
        elif line.startswith("-"):
            cur.removed.append(line[1:])
        elif line.startswith("\\"):
            pass
        else:
            new_ln += 1
    return files


def _git(args, root, timeout=20.0):
    return dd.run(["git", "-c", "core.quotepath=off"] + args, cwd=str(root), timeout=timeout)


def _is_binary(data):
    return b"\0" in data[:8000]


def _untracked_as_diff(root):
    listing = _git(["ls-files", "--others", "--exclude-standard"], root)
    if not listing:
        return ""
    chunks, total = [], 0
    # Untracked files under .claude/ are tool configuration (for example a freshly installed skill), not the change.
    paths = [p for p in listing.splitlines()
             if not p.startswith(".claude/") and "__pycache__/" not in p and not p.endswith((".pyc", ".pyo"))]
    for rel in paths[:MAX_UNTRACKED]:
        p = Path(root) / rel
        try:
            if not p.is_file() or p.stat().st_size > MAX_UNTRACKED_BYTES:
                continue
            data = p.read_bytes()
        except Exception:
            continue
        if _is_binary(data):
            chunks.append("diff --git a/%s b/%s\nnew file mode 100644\nBinary files /dev/null and b/%s differ"
                          % (rel, rel, rel))
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        total += len(data)
        if total > MAX_UNTRACKED_BYTES * 5:
            break
        body = "\n".join("+" + l for l in lines)
        chunks.append("diff --git a/%s b/%s\nnew file mode 100644\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1,%d @@\n%s"
                      % (rel, rel, rel, len(lines), body))
    return "\n".join(chunks)


def _diff(root, staged):
    if staged:
        return _git(["diff", "--no-color", "--no-ext-diff", "--staged"], root)
    has_head = _git(["rev-parse", "--verify", "--quiet", "HEAD"], root) is not None
    base = ["diff", "--no-color", "--no-ext-diff", "HEAD"] if has_head else ["diff", "--no-color", "--no-ext-diff", "--cached"]
    tracked = _git(base, root)
    if tracked is None:
        return None
    untracked = _untracked_as_diff(root)
    return tracked + ("\n" if tracked and untracked else "") + untracked


def _read_file(ref, root, cwd):
    candidates = [Path(ref)] if Path(ref).is_absolute() else [Path(cwd) / ref, Path(root) / ref]
    for p in candidates:
        try:
            if p.is_file():
                if p.stat().st_size > MAX_FILE_BYTES:
                    return None, "file is larger than 2 MB: %s" % ref
                data = p.read_bytes()
                if _is_binary(data):
                    return None, "binary file: %s" % ref
                return (p, data.decode("utf-8", "replace")), None
        except Exception:
            continue
    return None, None


def resolve(item_id, raw, root, cwd=None):
    """Turn the raw input text of one item into an Item."""
    cwd = cwd or root
    item = Item(item_id)
    item.root = root
    raw = raw or ""
    spans, free = [], []
    has_diff = False

    for m in (REF.finditer(raw) if REFS else ()):
        token = m.group(1)
        if token.startswith("DIFF"):
            staged = "--staged" in token
            text = _diff(root, staged)
            if text is None:
                item.error = "could not read input: %s (is this a git repository?)" % token
                return item
            item.diff_text += ("\n" if item.diff_text else "") + text
            item.refs.append(token)
            has_diff = True
            spans.append(m.span())
        elif token.startswith("COMMIT"):
            rev = m.group(2)
            if not SAFE_REV.match(rev or ""):
                item.error = "could not read input: invalid commit reference"
                return item
            text = _git(["show", "--no-color", "--no-ext-diff", "--format=%B%n---dev-decide-patch---", "--patch", rev], root)
            if text is None:
                item.error = "could not read input: COMMIT %s" % rev
                return item
            msg, _, patch = text.partition("---dev-decide-patch---")
            item.commit_message = msg.strip()
            item.diff_text += ("\n" if item.diff_text else "") + patch.strip()
            item.refs.append("COMMIT " + rev)
            has_diff = True
            spans.append(m.span())
        else:
            ref = m.group(3)
            found, err = _read_file(ref, root, cwd)
            if err:
                item.error = "could not read input: " + err
                return item
            if found is None:
                item.notes.append("@%s not found; treated as text" % ref)
                continue
            p, content = found
            rel = dd.norm_path(str(p), root)
            fc = FileChange(rel)
            fc.status = "file"
            fc.added = content.splitlines()
            fc.added_ln = list(range(1, len(fc.added) + 1))
            item.files.append(fc)
            item._current[rel] = content
            item.refs.append("@" + rel)
            spans.append(m.span())

    if not REFS and re.search(r"(?<!\S)(DIFF|COMMIT\s+\S+)(?!\S)", raw):
        item.notes.append("DIFF and COMMIT are not read on this server; send the diff itself in \"diff\"")
    if has_diff:
        item.files = parse_diff(item.diff_text) + item.files
    pos = 0
    for a, b in spans:
        free.append(raw[pos:a])
        pos = b
    free.append(raw[pos:])
    item.text = re.sub(r"[ \t]+", " ", "".join(free)).strip()

    if has_diff:
        item.kind = "mixed" if item.text else "diff"
        if not item.files and not item.text:
            item.error = "%s is empty: there are no changes to decide about" % " and ".join(item.refs)
            return item
    elif item.files:
        item.kind = "mixed" if item.text else "file"
    elif item.text:
        item.kind = "text"
    else:
        item.kind = "empty"
    return item


def attach_diff(item, text):
    """Add a unified diff sent inline (the JSON "diff" field), parsed exactly like DIFF."""
    if item.error or not text or not text.strip():
        return item
    item.diff_text += ("\n" if item.diff_text else "") + text
    item.files = parse_diff(text) + item.files
    item.refs.append("diff")
    if not any(f.status != "file" for f in item.files):
        item.error = "\"diff\" is not a unified diff (expected lines like \"diff --git a/... b/...\")"
        return item
    item.kind = "mixed" if item.text else "diff"
    return item


def llm_view(item, max_chars=60000, priority=None):
    """Text the model sees for one item, trimmed to max_chars with the riskiest files first."""
    parts = []
    if item.commit_message:
        parts.append("Commit message:\n" + item.commit_message[:2000])
    if item.text:
        parts.append(("Text:\n" if item.files else "") + item.text[:max_chars])
    budget = max_chars - sum(len(p) for p in parts)
    if item.files:
        files = list(item.files)
        if priority:
            files.sort(key=priority)
        if item.kind in ("diff", "mixed") and item.diff_text and len(item.diff_text) <= budget:
            parts.append("Diff:\n" + item.diff_text)
        else:
            stat = ["%s (%s, +%d -%d)" % (f.path, f.status, len(f.added), len(f.removed)) for f in item.files]
            parts.append("Changed files:\n" + "\n".join(stat[:200]))
            budget -= len(parts[-1])
            for f in files:
                if budget <= 500:
                    parts.append("[remaining files omitted to fit the size limit]")
                    break
                if f.binary:
                    continue
                if f.status == "file":
                    body = "\n".join("%d: %s" % (n, l) for n, l in zip(f.added_ln, f.added))
                    header = "File %s:\n" % f.path
                else:
                    body = "\n".join(["+" + l for l in f.added] + ["-" + l for l in f.removed])
                    header = "Changes in %s (added lines with +, removed with -):\n" % f.path
                chunk = header + body
                if len(chunk) > budget:
                    chunk = chunk[:budget - 40] + "\n[truncated]"
                parts.append(chunk)
                budget -= len(chunk)
    return "\n\n".join(parts) if parts else "(no input)"
