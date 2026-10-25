#!/usr/bin/env python3
"""Keep what a server learns across restarts, in a private git repository.

Free hosting (Render, Koyeb) wipes the disk on every restart, sleep and deploy, so the learning loop starts over
each time. With these environment variables, serve.py restores the last saved state when it starts, saves again
every few minutes when something changed, and once more when it stops:

  DEV_DECIDE_STATE_REPO       git URL of a private repository, e.g. git@github.com:you/dev-decide-state.git
  DEV_DECIDE_STATE_KEY_B64    that repository's deploy key (the private key, base64-encoded on one line)
  DEV_DECIDE_STATE_KEY_FILE   ...or the path of the key file instead (e.g. a Render secret file)
  DEV_DECIDE_STATE_EVERY_MIN  minutes between saves (default 10)

Saved: models (with their archived versions), the learning state, labels (hashed features, never text),
challengers, recall memory, and your corrections with their text removed. Never saved: cached answers, the
decision log, recent decisions and the spot-check queue. The repository keeps a single commit that is replaced
on each save, so it does not grow. SSH only talks to github.com with GitHub's published host keys.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ddcore as dd  # noqa: E402

# github.com's SSH host keys, from https://api.github.com/meta (pinned: no trust-on-first-use)
GITHUB_HOST_KEYS = (
    "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
    "github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9n"
    "kt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=\n"
)
INCLUDE_DIRS = ("models/", "learn/challengers/", "learn/episodes/")
INCLUDE_FILES = ("learn/state.json", "learn/labels.jsonl", "learn/history.jsonl", "learn/auto_presets.json",
                 "learn/memory.jsonl", "labels.jsonl")
SKIP_SUFFIXES = (".tmp", ".held", ".lock", ".new")
BRANCH = "main"


def log(msg):
    sys.stderr.write("dev-decide state: %s\n" % msg)


def wanted(rel):
    if rel.endswith(SKIP_SUFFIXES) or "/." in "/" + rel:
        return False
    return rel in INCLUDE_FILES or any(rel.startswith(d) for d in INCLUDE_DIRS)


class StateSync(object):
    def __init__(self, cwd, repo, key, every_min=10.0):
        self.root = Path(dd.project_root(cwd))
        self.state = self.root / ".claude" / "dev-decide"
        self.repo = repo
        self.every = max(1.0, float(every_min)) * 60
        self.work = Path(tempfile.mkdtemp(prefix="dd-state-"))
        self.clone = self.work / "repo"
        keyfile, hosts = self.work / "key", self.work / "known_hosts"
        keyfile.write_bytes(key if key.endswith(b"\n") else key + b"\n")
        os.chmod(str(keyfile), 0o600)
        hosts.write_text(GITHUB_HOST_KEYS, encoding="utf-8")
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                        GIT_SSH_COMMAND='ssh -i "%s" -o IdentitiesOnly=yes -o UserKnownHostsFile="%s" '
                                        '-o StrictHostKeyChecking=yes -o BatchMode=yes' % (keyfile, hosts),
                        GIT_AUTHOR_NAME="dev-decide server", GIT_AUTHOR_EMAIL="dev-decide@localhost",
                        GIT_COMMITTER_NAME="dev-decide server", GIT_COMMITTER_EMAIL="dev-decide@localhost")
        self.last_sig = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.ready = False

    @classmethod
    def from_env(cls, cwd):
        repo = (os.environ.get("DEV_DECIDE_STATE_REPO") or "").strip()
        if not repo:
            return None
        key = None
        if os.environ.get("DEV_DECIDE_STATE_KEY_B64"):
            try:
                key = base64.b64decode(os.environ["DEV_DECIDE_STATE_KEY_B64"].strip())
            except ValueError:
                log("DEV_DECIDE_STATE_KEY_B64 is not valid base64; state is not kept")
                return None
        elif os.environ.get("DEV_DECIDE_STATE_KEY_FILE"):
            try:
                key = Path(os.environ["DEV_DECIDE_STATE_KEY_FILE"]).read_bytes()
            except OSError:
                log("cannot read DEV_DECIDE_STATE_KEY_FILE; state is not kept")
                return None
        if not key or not shutil.which("git"):
            log("needs git and a deploy key (DEV_DECIDE_STATE_KEY_B64 or _FILE); state is not kept")
            return None
        return cls(cwd, repo, key, os.environ.get("DEV_DECIDE_STATE_EVERY_MIN") or 10)

    def _git(self, *args, timeout=90):
        try:
            p = subprocess.run(["git"] + list(args), cwd=str(self.clone if self.clone.exists() else self.work),
                               env=self.env, capture_output=True, text=True, timeout=timeout)
            return p.returncode == 0, (p.stderr or p.stdout or "").strip()[-300:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, type(exc).__name__

    def files(self):
        out = []
        if self.state.is_dir():
            for p in sorted(self.state.rglob("*")):
                if p.is_file():
                    rel = p.relative_to(self.state).as_posix()
                    if wanted(rel):
                        out.append((rel, p))
        return out

    def signature(self):
        sig = []
        for rel, p in self.files():
            try:
                st = p.stat()
                sig.append((rel, st.st_size, st.st_mtime_ns))
            except OSError:
                pass
        return tuple(sig)

    def restore(self):
        # Fetch the state branch itself, whatever the remote's default branch is (an empty repository has none).
        self.clone.mkdir(parents=True, exist_ok=True)
        self._git("init", "-q")
        self._git("remote", "add", "origin", self.repo)
        fetched, _ = self._git("fetch", "-q", "--depth", "1", "origin", BRANCH)
        if fetched:
            self._git("checkout", "-q", "-B", BRANCH, "FETCH_HEAD")
        else:
            reachable, msg = self._git("ls-remote", "origin")
            if not reachable:
                log("could not reach %s (%s); starting without saved state" % (self.repo, msg))
                return False
            self._git("symbolic-ref", "HEAD", "refs/heads/" + BRANCH)  # nothing saved yet: the first save creates it
        self.ready = True
        saved = self.clone / "state"
        n = 0
        if saved.is_dir():
            for p in sorted(saved.rglob("*")):
                rel = p.relative_to(saved).as_posix()
                if p.is_file() and wanted(rel):
                    dest = self.state / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(p), str(dest))
                    n += 1
        dd.state_dir(self.root)  # its .gitignore
        try:
            import fastmodel
            fastmodel.seed_models(self.root)  # newer starter models still replace untouched old ones
        except Exception:
            pass
        self.last_sig = self.signature()
        log("restored %d file(s) from %s" % (n, self.repo) if n else "no saved state yet in %s" % self.repo)
        return True

    def save(self):
        if not self.ready:
            return False
        with self.lock:
            sig = self.signature()
            if sig == self.last_sig:
                return True
            target = self.clone / "state"
            import learn
            with learn.Lock(learn.Paths(self.root).lock, wait=5.0):  # a consistent snapshot of the learning state
                if target.exists():
                    shutil.rmtree(str(target))
                for rel, p in self.files():
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if rel == "learn/memory.jsonl":  # corrections keep their hashed form, never their text
                        rows = []
                        for line in p.read_text(encoding="utf-8").splitlines():
                            try:
                                r = json.loads(line)
                            except ValueError:
                                continue
                            r.pop("text", None)
                            rows.append(json.dumps(r, separators=(",", ":")))
                        dest.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
                    else:
                        shutil.copy2(str(p), str(dest))
            (self.clone / "README.md").write_text(
                "What a dev-decide server has learned (models and hashed labels, no input text). Written by "
                "statesync.py; each save replaces the single commit.\n", encoding="utf-8")
            self._git("add", "-A")
            has_head, _ = self._git("rev-parse", "--verify", "HEAD")
            ok, msg = self._git("commit", "--amend", "--no-edit", "-q", "-m", "dev-decide state " + time.strftime(
                "%Y-%m-%d %H:%M:%S")) if has_head else self._git("commit", "-q", "-m", "dev-decide state")
            if not ok and "nothing to commit" not in msg:
                log("commit failed: %s" % msg)
                return False
            ok, msg = self._git("push", "--force", "-q", "origin", "HEAD:" + BRANCH)
            if not ok:
                log("push failed: %s" % msg)
                return False
            self.last_sig = sig
            log("saved %d file(s)" % len(sig))
            return True

    def start(self):
        def loop():
            while not self.stop_event.wait(self.every):
                try:
                    self.save()
                except Exception as exc:
                    log("save failed: %s" % type(exc).__name__)

        threading.Thread(target=loop, name="dev-decide-state", daemon=True).start()

    def stop(self):
        self.stop_event.set()
        try:
            self.save()
        except Exception as exc:
            log("final save failed: %s" % type(exc).__name__)
        shutil.rmtree(str(self.work), ignore_errors=True)
