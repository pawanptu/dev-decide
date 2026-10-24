"""Prompts for writing and labeling bootstrap data, their parsers, and an offline mock teacher.

Data quality is most of the accuracy, so the prompts push for diversity (styles, stacks,
lengths, typos, hard near-miss cases) and the labeling prompts are blind: the labeler
never sees what the writer was aiming for. Examples where writer and labeler disagree
are dropped as ambiguous.
"""
import random
import re

ISSUE_TYPES = {
    "bug": "something is broken: a crash, an error, wrong output, data loss or a regression",
    "feature_request": "asks for new behavior or a change to how something intentionally works",
    "question": "asks how to do something or why something behaves as it does",
    "docs": "about documentation: missing, wrong, outdated or unclear docs",
    "support": "an account, billing, access or order problem for a specific user, not a code defect",
    "duplicate_or_noise": "+1s, 'same here', spam, empty or test posts, or explicit duplicates",
}
SEVERITIES = {
    "sev1_outage": "outage or data loss affecting many users",
    "sev2_major": "a core feature is broken with no workaround",
    "sev3_minor": "works with a workaround, or affects few users or an edge case",
    "sev4_cosmetic": "visual, wording or layout only",
    "not_a_problem": "works as intended, or it is a question or request rather than a defect",
}
DOMAINS = {
    "frontend": "web UI code, components, styling and browser behavior",
    "backend": "server-side APIs, services and business logic",
    "mobile": "iOS, Android, React Native or Flutter apps",
    "desktop": "Electron, Tauri or native desktop apps",
    "database": "schemas, queries, migrations, indexes and ORMs",
    "data_engineering": "pipelines, ETL, Airflow, dbt, Spark, Kafka and warehouses",
    "analytics_bi": "dashboards, metrics, BI tools and reporting",
    "data_science": "notebooks, pandas analysis, statistics and EDA",
    "ml_classic": "scikit-learn, gradient boosting and classical models",
    "deep_learning": "neural networks, PyTorch or TensorFlow training, GPUs and fine-tuning",
    "llm_app": "apps that call LLM APIs: prompts, chatbots, completions",
    "rag": "retrieval-augmented generation, embeddings and vector stores",
    "generative_media": "image, audio or video generation",
    "ai_agents": "tool-using agents, multi-agent systems and MCP",
    "mlops": "model serving, registries, feature stores and monitoring",
    "computer_vision": "image and video understanding, detection and OCR",
    "nlp_speech": "text processing, tokenization, speech recognition and TTS",
    "devops_infra": "CI/CD, Docker, Kubernetes, Terraform and deployment",
    "cloud": "AWS, GCP or Azure services, IAM and serverless",
    "security": "vulnerabilities, auth hardening, encryption and secrets",
    "embedded_iot": "firmware, microcontrollers, sensors and IoT protocols",
    "robotics": "robots, ROS, motion planning and actuators",
    "ar_vr": "augmented and virtual reality apps and devices",
    "game": "game engines, gameplay and game rendering",
    "blockchain_web3": "smart contracts, wallets and chains",
    "hpc_scientific": "high-performance computing, simulations, MPI and numerics",
    "quantum": "quantum computing SDKs and circuits",
    "testing_qa": "test frameworks, flaky tests and QA processes",
    "networking": "protocols, DNS, sockets, proxies and VPNs",
    "os_systems": "kernels, drivers, filesystems and low-level systems",
    "tooling_cli": "command-line tools, developer tooling and build tools",
    "docs": "documentation itself",
}
RISK_SCALE = ("1 trivial and isolated (typo, comment, tests only); 2 local logic change covered by tests; "
              "3 shared code or user-visible behavior change; 4 public API, schema, auth, payments, concurrency, "
              "migration, or model and prompt behavior in production; 5 possible data loss, security exposure, "
              "or hard to reverse")
BREAKING_DEF = ("breaking means existing callers, public APIs, stored data, configuration or command-line "
                "behavior stop working as before")
RISK_KINDS = {
    1: ["fix a typo in documentation", "add or update unit tests only", "reword code comments",
        "reformat code without changing behavior", "update an example in a README"],
    2: ["fix a small bug inside one function", "add logging or metrics", "rename a private helper",
        "bump a patch version of a dependency", "small refactor inside one module"],
    3: ["add a new optional endpoint or feature flag", "change user-visible UI behavior",
        "change a configuration default", "modify a CI workflow", "add a caching layer",
        "change a prompt or sampling parameter in an LLM call"],
    4: ["add a database migration that adds columns or indexes", "change authentication or session handling",
        "change a public function signature or API response shape", "change locking or concurrency",
        "change payment or billing logic", "major version bump of a core dependency",
        "give an AI agent a new tool that acts on external systems"],
    5: ["drop or rename a database column", "remove an authorization check or input validation",
        "rewrite or delete stored data in a migration", "disable TLS verification or another security control",
        "change how credentials are stored or transmitted"],
}
P_BREAKING = {1: 0.03, 2: 0.12, 3: 0.3, 4: 0.55, 5: 0.65}
STACKS = ["Python FastAPI service", "Django app", "TypeScript Node.js Express API", "React and Next.js frontend",
          "Go microservice", "Java Spring Boot service", "Rust command-line tool", "Ruby on Rails app",
          "Kotlin Android app", "Swift iOS app", "Terraform infrastructure", "GitHub Actions workflows",
          "PostgreSQL schema with SQL migrations", "Python machine learning code with PyTorch or scikit-learn",
          "Python LLM app using the Anthropic SDK", "TypeScript with the Prisma ORM"]
STYLES = ["terse commands", "detailed requests with background", "problems described as symptoms",
          "requests naming specific files, functions or identifiers", "messages from someone new to the codebase",
          "messages from a senior engineer", "requests with a pasted error message", "multi-step requests",
          "questions", "requests with typos and no punctuation"]

GEN_SYSTEM = ("You write realistic, varied training data for a software decision model. Follow the output "
              "format exactly and output nothing else: no introductions, explanations or summaries.")
LABEL_SYSTEM = ("You are a careful senior software engineer labeling data for a classifier. Judge each item "
                "only by its own content. Follow the output format exactly and output nothing else.")


def _clean_line(line):
    line = line.strip().strip("`").strip()
    line = re.sub(r"^(\d+[.):]\s*|[-*•]\s+)", "", line)
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'":
        line = line[1:-1]
    return line.strip()


# ---------------------------------------------------------------- route

def route_catalog_text(routes):
    lines = ["- %s: %s" % (r["name"], r.get("description") or r["name"].replace("-", " ")) for r in routes]
    return "\n".join(lines)


def route_gen(routes, target, n, batch_no, seed):
    style = STYLES[(batch_no + seed) % len(STYLES)]
    k = max(2, n // 5)
    head = "Task: route-generate (batch %d, seed %d)\n\nRoutes:\n%s\n\n" % (batch_no, seed, route_catalog_text(routes))
    if target == "none":
        body = ("Write %d requests a developer might send to an AI coding assistant that NONE of these routes should "
                "handle: general questions, work outside every route's scope, other tools' jobs, or non-software "
                "requests. About %d should be near misses that sound related to a route but are not its job.\n"
                "Lean toward this style: %s.\n" % (n, k, style))
    else:
        body = ("Write %d different requests a developer might send to an AI coding assistant that the route `%s` "
                "should handle and no other route should.\nVary length (3 words to 3 sentences), tone and "
                "precision; include file names, identifiers or error messages in some; a few with typos.\nAbout %d "
                "should be hard cases that mention words or tools associated with another route but still belong "
                "to `%s`.\nLean toward this style: %s.\nNever mention the route name itself.\n"
                % (n, target, k, target, style))
    return GEN_SYSTEM, head + body + "\nWrite one request per line. No numbering, quotes or blank lines."


def parse_route_gen(text):
    out = []
    for line in (text or "").splitlines():
        line = _clean_line(line)
        if 3 <= len(line) <= 600 and not line.lower().startswith(("here are", "sure", "task:")):
            out.append(line)
    return out


def route_label(routes, texts):
    lines = ["Task: route-label", "", "Routes:", route_catalog_text(routes), "- none: no route fits", "",
             "For each numbered request choose the single best route, or none. Reply with one line per request:",
             "<number> <route> <confidence>",
             "where confidence is your probability from 0 to 1 that the route is right. No other text.", "",
             "Requests:"]
    for i, t in enumerate(texts, 1):
        lines.append("%d. %s" % (i, t.replace("\n", " ")[:800]))
    return LABEL_SYSTEM, "\n".join(lines)


LABEL_LINE = re.compile(r"^\s*(\d+)[.):]?\s+(.*)$")
NUM = re.compile(r"^(?:\d+(?:\.\d+)?|\.\d+)%?$")


def _prob(tok, default=0.7):
    tok = tok.strip().rstrip(",;")
    if not NUM.match(tok):
        return default
    v = float(tok.rstrip("%"))
    if tok.endswith("%") or v > 1.0:
        v /= 100.0
    return min(max(v, 0.0), 1.0)


def parse_route_label(text, n, valid):
    out = {}
    for line in (text or "").splitlines():
        m = LABEL_LINE.match(line.strip().strip("`"))
        if not m:
            continue
        idx = int(m.group(1))
        toks = m.group(2).replace("|", " ").split()
        if not (1 <= idx <= n) or not toks:
            continue
        label = toks[0].strip("`*,;:")
        if label in valid:
            out[idx] = (label, _prob(toks[1]) if len(toks) > 1 else 0.7)
    return out


# ---------------------------------------------------------------- triage

def triage_gen(targets, batch_no, seed):
    lines = ["Task: triage-generate (batch %d, seed %d)" % (batch_no, seed), "",
             "Write %d realistic items from a software project's tracker: GitHub issues, Jira tickets or support "
             "tickets. Each must match its target type and domain:" % len(targets)]
    for i, (t, d) in enumerate(targets, 1):
        lines.append("%d. type=%s, domain=%s" % (i, t, d))
    lines += ["", "Types:"] + ["- %s: %s" % kv for kv in ISSUE_TYPES.items()]
    lines += ["", "Domains used above:"] + ["- %s: %s" % (d, DOMAINS[d]) for d in sorted(set(d for _, d in targets))]
    lines += ["", "Severity scale:"] + ["- %s: %s" % kv for kv in SEVERITIES.items()]
    lines += ["", "Choose a realistic severity for each item and spread severities across the batch. Vary length "
              "(one line to a short paragraph), tone, detail (versions, stack traces, steps to reproduce) and writing "
              "quality. Never name the type or domain label in the text.",
              "Reply with exactly one line per item: <number> | <severity> | <item text>",
              "Keep each item on one line, using spaces instead of line breaks."]
    return GEN_SYSTEM, "\n".join(lines)


def parse_triage_gen(text, n):
    out = {}
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.strip().strip("`").split("|")]
        if len(parts) < 3:
            continue
        m = re.match(r"^(\d+)", parts[0])
        if not m:
            continue
        idx, sev, body = int(m.group(1)), parts[1].strip("`*"), " | ".join(parts[2:]).strip()
        if 1 <= idx <= n and sev in SEVERITIES and 3 <= len(body) <= 2000:
            out[idx] = (sev, body)
    return out


def triage_label(texts):
    lines = ["Task: triage-label", "", "Classify each item on three fields.", "", "Types:"]
    lines += ["- %s: %s" % kv for kv in ISSUE_TYPES.items()]
    lines += ["", "Severities:"] + ["- %s: %s" % kv for kv in SEVERITIES.items()]
    lines += ["", "Domains:"] + ["- %s: %s" % kv for kv in DOMAINS.items()]
    lines += ["", "Reply with one line per item:",
              "<number> <type>:<confidence> <severity>:<confidence> <domain>:<confidence>",
              "where each confidence is your probability from 0 to 1. No other text.", "", "Items:"]
    for i, t in enumerate(texts, 1):
        lines.append("%d. %s" % (i, t.replace("\n", " ")[:1500]))
    return LABEL_SYSTEM, "\n".join(lines)


def parse_triage_label(text, n):
    out = {}
    for line in (text or "").splitlines():
        m = LABEL_LINE.match(line.strip().strip("`"))
        if not m:
            continue
        idx = int(m.group(1))
        pairs = re.findall(r"([a-z_0-9]+)\s*[:=]\s*([0-9.]+%?)", m.group(2))
        if not (1 <= idx <= n):
            continue
        got = {}
        for label, p in pairs:
            if label in ISSUE_TYPES and "issue_type" not in got:
                got["issue_type"] = (label, _prob(p))
            elif label in SEVERITIES and "severity" not in got:
                got["severity"] = (label, _prob(p))
            elif label in DOMAINS and "domain" not in got:
                got["domain"] = (label, _prob(p))
        if got:
            out[idx] = got
    return out


# ---------------------------------------------------------------- pull requests

def pr_targets(n, rnd):
    targets = []
    for _ in range(n):
        risk = rnd.randint(1, 5)
        breaking = rnd.random() < P_BREAKING[risk]
        targets.append((risk, breaking, rnd.choice(RISK_KINDS[risk]), rnd.choice(STACKS)))
    return targets


def pr_gen(targets, batch_no, seed):
    lines = ["Task: pr-generate (batch %d, seed %d)" % (batch_no, seed), "",
             "Write %d realistic code changes as unified git diffs. Each must match its target:" % len(targets)]
    for i, (risk, breaking, kind, stack) in enumerate(targets, 1):
        lines.append("%d. risk=%d, breaking=%s, change: %s, stack: %s" % (i, risk, "yes" if breaking else "no",
                                                                           kind, stack))
    lines += ["", "Risk scale: " + RISK_SCALE + ".", "Breaking: " + BREAKING_DEF + ".", "",
              "Rules for every diff:",
              "- Real git format: a line `diff --git a/<path> b/<path>`, then `--- a/<path>` and `+++ b/<path>` "
              "(use /dev/null and `new file mode 100644` for new files), then hunks starting `@@ -l,s +l,s @@` "
              "with lines beginning +, - or a space.",
              "- 5 to 40 changed lines in one to three files, with realistic paths and code for the stack.",
              "- No comments that state the risk or say whether it is breaking: the code must speak for itself.",
              "", "Start each example with a line `=== <number> ===` and output nothing else."]
    return GEN_SYSTEM, "\n".join(lines)


SECTION = re.compile(r"^=+\s*(?:example\s*)?(\d+)\s*=+\s*$", re.I)


def parse_pr_gen(text, n):
    out, cur, buf = {}, None, []

    def flush():
        if cur is not None and 1 <= cur <= n:
            body = "\n".join(buf).strip("\n")
            body = re.sub(r"^```[a-z]*\n?|\n?```$", "", body.strip())
            if "diff --git" in body:
                out[cur] = body[body.index("diff --git"):]

    for line in (text or "").splitlines():
        m = SECTION.match(line.strip())
        if m:
            flush()
            cur, buf = int(m.group(1)), []
        elif cur is not None:
            buf.append(line)
    flush()
    return out


def pr_label(diffs):
    lines = ["Task: pr-label", "", "Rate each code change.", "Risk scale: " + RISK_SCALE + ".",
             "Breaking: " + BREAKING_DEF + ".", "",
             "Reply with one line per change:",
             "<number> risk=<1-5>:<confidence> breaking=<yes|no>:<probability it is breaking>",
             "Confidences are probabilities from 0 to 1. No other text.", ""]
    for i, d in enumerate(diffs, 1):
        lines.append("=== %d ===" % i)
        lines.append(d[:6000])
    return LABEL_SYSTEM, "\n".join(lines)


def parse_pr_label(text, n):
    out = {}
    for line in (text or "").splitlines():
        m = LABEL_LINE.match(line.strip().strip("`"))
        if not m:
            continue
        idx = int(m.group(1))
        if not (1 <= idx <= n):
            continue
        rest = m.group(2)
        rm = re.search(r"risk\s*[=:]\s*([1-5])(?:\s*[:/]\s*([0-9.]+%?))?", rest)
        bm = re.search(r"breaking\s*[=:]\s*(yes|no|true|false)(?:\s*[:/]\s*([0-9.]+%?))?", rest, re.I)
        got = {}
        if rm:
            got["risk"] = (rm.group(1), _prob(rm.group(2)) if rm.group(2) else 0.7)
        if bm:
            yes = bm.group(1).lower() in ("yes", "true")
            p = _prob(bm.group(2), 0.8 if yes else 0.2) if bm.group(2) else (0.8 if yes else 0.2)
            if yes and p < 0.5:  # "breaking=yes:0.3" is contradictory; trust the word
                p = 1 - p
            if not yes and p > 0.5:
                p = 1 - p
            got["breaking_change"] = ("true" if yes else "false", max(p, 1 - p))
        if got:
            out[idx] = got
    return out


# ---------------------------------------------------------------- offline mock teacher

ROUTE_FILL = ["please", "can you", "quickly", "for the release", "in our repo", "asap", "when you get a chance",
              "for the new service", "before friday", "in staging"]
TYPE_TEMPLATES = {
    "bug": ["crashes with an error when {d}", "returns wrong results after the update in {d}",
            "broken since yesterday, exception in {d}"],
    "feature_request": ["please add support for {d}", "would be great to have a new option for {d}",
                        "feature request: export and filters for {d}"],
    "question": ["how do I configure {d}?", "is it possible to use {d} with our setup?",
                 "why does {d} behave like this?"],
    "docs": ["the documentation for {d} is outdated", "readme is missing setup steps for {d}",
             "typo in the {d} docs page"],
    "support": ["I cannot access my account, billing page shows an error", "please refund my order, charged twice",
                "my subscription login is locked"],
    "duplicate_or_noise": ["+1", "same here", "duplicate of #123"],
}
SEV_WORDS = {"sev1_outage": "all users down outage", "sev2_major": "no workaround blocking",
             "sev3_minor": "workaround exists some users", "sev4_cosmetic": "misaligned colour typo",
             "not_a_problem": "works as intended"}


def _rnd(*parts):
    return random.Random("|".join(str(p) for p in parts))


def mock_reply(system, user):
    """A deterministic stand-in for Claude that understands this module's prompts (for tests)."""
    first = user.splitlines()[0] if user else ""
    rnd = _rnd(user[:4000])
    if first.startswith("Task: route-generate"):
        target = re.search(r"route `([^`]+)` should handle", user)
        n = int(re.search(r"Write (\d+)", user).group(1))
        if target:
            name = target.group(1)
            desc = re.search(r"^- %s: (.*)$" % re.escape(name), user, re.M).group(1)
            words = [w for w in re.findall(r"[a-z]+", desc.lower()) if len(w) > 3]
        else:
            words = ["weather", "recipe", "holiday", "music", "poem", "football", "history", "translate"]
        return "\n".join("%s %s %s %d" % (rnd.choice(ROUTE_FILL), " ".join(rnd.sample(words, min(3, len(words)))),
                                           rnd.choice(ROUTE_FILL), rnd.randint(1, 999)) for _ in range(n))
    if first.startswith("Task: route-label"):
        routes = dict(re.findall(r"^- ([\w.:-]+): (.*)$", user, re.M))
        reqs = re.findall(r"^(\d+)\. (.*)$", user, re.M)
        lines = []
        for idx, text in reqs:
            words = set(re.findall(r"[a-z]+", text.lower()))
            scores = {r: len(words & set(re.findall(r"[a-z]+", d.lower()))) for r, d in routes.items() if r != "none"}
            best = max(scores, key=scores.get) if scores and max(scores.values()) > 0 else "none"
            if rnd.random() < 0.08:
                best = rnd.choice(list(routes))
            lines.append("%s %s %.2f" % (idx, best, rnd.uniform(0.6, 0.97)))
        return "\n".join(lines)
    if first.startswith("Task: triage-generate"):
        targets = re.findall(r"^(\d+)\. type=(\w+), domain=(\w+)$", user, re.M)
        lines = []
        for idx, t, d in targets:
            sev = rnd.choice(list(SEVERITIES))
            text = rnd.choice(TYPE_TEMPLATES[t]).format(d=DOMAINS[d].split(",")[0])
            lines.append("%s | %s | %s %s" % (idx, sev, text, SEV_WORDS[sev]))
        return "\n".join(lines)
    if first.startswith("Task: triage-label"):
        items = re.findall(r"^(\d+)\. (.*)$", user, re.M)
        lines = []
        for idx, text in items:
            low = text.lower()
            t = next((k for k, tpls in TYPE_TEMPLATES.items()
                      if any(tpl.split("{")[0].strip()[:12] in low for tpl in tpls)), "bug")
            s = next((k for k, w in SEV_WORDS.items() if w in low), "sev3_minor")
            d = max(DOMAINS, key=lambda k: sum(w in low for w in DOMAINS[k].lower().split(",")[0].split()))
            if rnd.random() < 0.07:
                t = rnd.choice(list(ISSUE_TYPES))
            lines.append("%s %s:%.2f %s:%.2f %s:%.2f" % (idx, t, rnd.uniform(.6, .95), s, rnd.uniform(.6, .95),
                                                        d, rnd.uniform(.6, .95)))
        return "\n".join(lines)
    if first.startswith("Task: pr-generate"):
        targets = re.findall(r"^(\d+)\. risk=(\d), breaking=(yes|no), change: (.*), stack: (.*)$", user, re.M)
        out = []
        for idx, risk, breaking, kind, stack in targets:
            path = {"1": "docs/guide.md", "2": "src/util/helpers.py", "3": "src/api/routes.py",
                    "4": "db/migrations/0042_add_index.sql", "5": "db/migrations/0043_drop_column.sql"}[risk]
            body = {"1": ["-Teh setup guide", "+The setup guide"],
                    "2": ["-    return total", "+    return round(total, 2)"],
                    "3": ["+@app.get('/v1/reports')", "+def reports(): return list_reports()"],
                    "4": ["+ALTER TABLE orders ADD COLUMN customer_ref text;", "+CREATE INDEX idx_ref ON orders(customer_ref);"],
                    "5": ["+ALTER TABLE users DROP COLUMN legacy_email;", "+DELETE FROM sessions;"]}[risk]
            if breaking == "yes":
                body = body + ["-def get_user(id):", "+def get_user(id, opts):"]
            out.append("=== %s ===\ndiff --git a/%s b/%s\n--- a/%s\n+++ b/%s\n@@ -1,3 +1,4 @@\n %s\n%s"
                       % (idx, path, path, path, path, "# " + kind[:40], "\n".join(body)))
        return "\n".join(out)
    if first.startswith("Task: pr-label"):
        chunks = re.split(r"^=== (\d+) ===$", user, flags=re.M)
        lines = []
        for i in range(1, len(chunks) - 1, 2):
            idx, d = chunks[i], chunks[i + 1]
            risk = 5 if "DROP COLUMN" in d or "DELETE FROM" in d else 4 if "ALTER TABLE" in d else \
                1 if ".md" in d else 3 if "@app" in d else 2
            if rnd.random() < 0.1:
                risk = min(5, max(1, risk + rnd.choice((-1, 1))))
            br = "def get_user(id, opts)" in d
            lines.append("%s risk=%d:%.2f breaking=%s:%.2f" % (idx, risk, rnd.uniform(.6, .9),
                                                                "yes" if br else "no", rnd.uniform(.7, .95)))
        return "\n".join(lines)
    return ""
