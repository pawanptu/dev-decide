# dev-decide 5.2.0

A Jev-style decision engine (open source, local) for software and AI work, packaged as a Claude Code skill plus a
command-line tool, an opt-in routing hook and a small HTTP API. It decides locally in milliseconds
when it can, asks Claude when it can't, and learns from every answer.

**New in 5.2:** a decision graph of which agent or skill was suggested for which task, what Claude really used and how long each step took (a page at `http://127.0.0.1:47321/graph` and the `dev-decide-graph` side panel), a redesigned web console, and retrained `breaking_change` and route models. See `../CHANGELOG.md`.

**New in 5.1:** a routing hint in about 5 ms, routing by meaning, example agents with their route model, a demo token and bring-your-own-key for the web page, learning that survives restarts, GitHub Actions and retrained models. See `../CHANGELOG.md`.

**New in 5.0.1: the HTTP API can be deployed.** `serve.py` checks a token (`DEV_DECIDE_TOKEN`), refuses to
listen beyond localhost without one, stops reading the server's files (`DIFF`, `COMMIT`, `@path`) when it
listens beyond localhost, and accepts a pull request's diff inline (`"diff"`), with the same answers as `DIFF`.
Decisions are otherwise unchanged (99 of 99 test decisions identical to 5.0). `setup_deploy.md`, next to this
folder in the bundle, walks through running it locally and on free hosting.

**New in 5.0: it understands meaning, learns from each answer, tunes itself, and escalates hard cases.**
Every change below was measured before it shipped; numbers are in "5.0 measurements".
- **Understands meaning, where that helps.** A small sentence-embedding model (potion-base-8M, 30 MB, MIT,
  bundled) gives each input 256 meaning features, read by a pure-Python implementation (no numpy, no
  tokenizers package) that matches the reference library on all real test inputs. Each decision type chooses
  word features or word-plus-meaning features by cross-validated accuracy. Routing chose meaning and went from
  84.9% to 90.6% on held-out requests; domain went from 57.9% to 68.4%. Issue type and severity kept word
  features, which measured better for them.
- **Learns from each answer.** Besides the trained model, a fast memory recalls inputs like ones already
  labeled (by Claude, a spot check or you), so a request Claude answered once can be answered locally the next
  time. It is trusted per decision type only as far as its test-then-store record on your labels reaches the
  90% target, the newest label for an input wins, and when recall and the trained model disagree, Claude
  decides (and that answer becomes a label).
- **Tunes itself under change.** Confidence thresholds rise and fall per class with spot-check results;
  spot checks favour borderline, unfamiliar and recalled answers (reweighted, so accuracy estimates stay
  unbiased) and run four times as often after recent errors; two confirmed errors in one class make both local
  tiers distrust that class until fresh labels rebuild it. Inputs unlike anything the model was trained on go to
  Claude instead of getting a confident guess.
- **Stronger model for hard cases.** When Claude's fast model (Haiku) is unsure (confidence under 0.6) about a
  decision that matters (gates, routing, risk, severity, security and AI-safety checks), the question is
  re-asked to Sonnet, at most 30 calls a day. Its answers count 1.5x as labels.
- **Same off switches.** With `"learning": {"enabled": false}` and `"escalate": false` (or
  `DEV_DECIDE_LEARNING=0`, `DEV_DECIDE_ESCALATE=0`), 5.0 answers exactly like 3.1: 99 of 99 test decisions
  identical. With learning on and nothing learned yet, they are also identical.

**From 4.0: it learns from use.** Every decision teaches the local tier, in the spirit of Hermes Agent's learning loop, with
one difference: Hermes Agent writes new skills and memories but does not check that they help, while
dev-decide only switches to a new model after it has proven better on your own decisions.
- **Learns from every model answer.** When the local tier passes a question to Claude, Claude's answer becomes
  a training label. A challenger model (the current model plus a learned correction) takes one small update
  per label, in a few milliseconds, inside the normal call. Nothing runs in the background.
- **Switches only on evidence, and can undo.** Before learning from a label, the current model and the
  challenger both predict it, so their running scores are honest. The challenger takes over when it answers
  more inputs locally at the 90% accuracy target, or is clearly more accurate, without losing accuracy where
  the current model already answers. The old model is archived, and `rollback` restores it.
- **Spot checks.** About 1 in 20 local answers is re-asked to Claude, at most 20 a day, only when model
  calls are allowed. The checks ride along with a call that is being made anyway, so they add no wait. If
  spot checks show local answers clearly below target, the model pauses until a better one proves itself.
- **Teach it once.** `/dev-decide fix last issue_type=bug` corrects an answer. Near-identical inputs get the
  corrected answer immediately (memory tier). Similar inputs that go to Claude show it your correction as a
  precedent, and the challenger trains on it at triple weight.
- **New agents and recurring custom questions.** When you add an agent or skill, requests that look like it
  go to Claude until the route model has learned it. A custom question you ask repeatedly (for example
  `choose refund|dispute|other`) gets its own local model once it has proven itself.
- **Commands:** `fix`, `status`, `improve`, `rollback`, `promote`, `forget`, in Claude Code (`/dev-decide status`),
  in the terminal, and through the API (`POST /v1/feedback`, `GET /v1/learning`).
- **Off switch:** `"learning": {"enabled": false}` in the policy file, or `DEV_DECIDE_LEARNING=0`. With learning
  off, 4.0 answers exactly like 3.1 (99 of 99 test decisions identical, as they are with learning on and
  nothing learned yet).

**From 3.1: a stronger learned tier**, filled by the companion `dev-decide-kit`:
- A second featurizer, `f2`, adds character 3- and 4-grams, so *migrate*, *migration* and *migrations* share
  evidence. Models choose `f1` or `f2` per decision, whichever wins on calibration, accuracy and then speed.
- Compact binary model files (`<preset>.bin`, float32 weights) load in a few milliseconds instead of parsing JSON.
- Features are computed lazily, once per input, and only for the featurizers an enabled model needs.
- Feature selection is now deterministic across processes (ties broken by bucket id).
- Warm processes reuse loaded models, the agent and skill list (re-read within 2 s of an edit, at once when
  one is added or removed) and the git root, and expired cache entries are swept every 10 minutes instead of
  on every write. A routing request through the HTTP API went from about 11 ms to about 3 ms, with identical
  answers.
- Fully backward compatible: v3.0 models and logs keep working.

You ask a typed question (choose, score or verify), and it returns JSON: the answer, its
probabilities, a confidence, whether to accept, escalate or ask a human, and pass / review / block
for gated bundles such as PRs, deploys, AI changes and agent releases.

## 5.0 measurements

**Meaning features** (the same 821 bootstrapped examples as 4.0, retrained without new model calls; held-out
test sets):

| Decision | Features chosen | Held-out accuracy, 4.0 → 5.0 | Answered locally at 90% target (test), 4.0 → 5.0 |
|---|---|---|---|
| route | f4 (words + meaning) | 84.9% → 90.6% | 83% at 91% → 91% at 94% |
| domain (32 classes) | f3 (words + meaning) | 57.9% → 68.4% | 34% at 85% → 47% at 89% (not yet enabled) |
| issue_type | f1 (words) | 95.5% → 95.5% | 98% at 98% (unchanged) |
| severity | f1 (words) | 78.6% → 78.6% | unchanged, not enabled |
| risk | f2 (words + spelling) | 63.6% → 68.2% | none → 9% at 100% (not enabled) |

The pure-Python reader produced the same tokens as the reference library (model2vec 0.10) for every real input
tested (821 examples, including diffs) and 1,145 of 1,147 strings in total; the two differences are random
strings of rare Unicode characters, where Python's Unicode tables are newer than the tokenizer's.

**Learning from each answer and tuning itself** (simulation through the real engine: 900 decisions on the
bootstrapped issues with repeats and light rewording, several days, a teacher that knows the true labels, a
weak starting model; means over seeds):

| Stream | 5.0: answered locally | 5.0: local answers right | 4.0 behaviour: locally | 4.0: right |
|---|---|---|---|---|
| Steady (2 seeds) | 80% | 100.0% | 60% | 99.9% |
| Convention change halfway, question → support (3 seeds) | 79% | 96.6% | 57% | 97.8% |
| The 180 decisions right after that change | | 87.1% | | 91.9% |
| Teacher wrong 30% of the time | about 1% | 100% | about 1% | 100% |

So 5.0 answers about 20 points more locally at the same accuracy on a steady stream. After an abrupt
convention change it dips further than 4.0 for a while, because more local answers mean fewer labels to notice
the change from; spot checks and the class breaker then recover it. With a noisy teacher it refuses to automate.

**End to end.**
- **Unfamiliar input.** French text sent to the issue-type model went to Claude instead of getting a confident
  local guess.
- **Live escalation.** Haiku answered a vague severity question at 0.60. With the threshold raised to 0.7 for
  the test, Sonnet was asked and agreed: sev3_minor at 0.62. That took 7.6 s and $0.016 in total, and the
  label was stored with stronger weight.
- **Better routing.** The routing hook now routes "rename the user_email column to email in postgres without
  downtime" to db-migrate; 4.0 gave no hint for it.

**Speed** (warm process unless noted):
- Routing through the HTTP API: 4.1 ms. The meaning embedding adds about 0.8 ms.
- Triage through the HTTP API: 3.2 ms. Cache hits: 1.2–1.8 ms.
- The command line: 74 ms. The routing hook: 72 ms, as a new process each time; loading the meaning model adds
  about 11 ms to it.

## How it gets its speed

Each question goes to the fastest source that can be trusted:

| Tier | What answers | Typical time | Cost |
|---|---|---|---|
| **rule** | Exact local facts: only docs changed, a live Stripe key, a migration file, a CUDA OOM line, no AI code involved | ~3 ms for a short request and ~11 ms for a PR bundle read from git, in the API server; about 50 ms more as a new process (CLI, hook) | free |
| **learned** | Your trained fast model (hashed features + logistic regression), only above the threshold it proved on held-out data | under 1 ms on top of the analyzers | free |
| **llm** | One lean Haiku call for everything still open, with the local findings as evidence | ~2.5–4.5 s | ~$0.0004–0.002 |
| **cache** | The same request on the same input, policy and models | ~1.5 ms in the API server | free |
| **memory** | Your own correction of a near-identical input (similarity of hashed features at least 0.80) | under 1 ms | free |
| **episodic** | Inputs like ones already labeled, once that recall has proven accurate for the decision type | about 1 ms | free |
| **llm-strong** | Sonnet, for unsure answers to decisions that matter (at most 30 calls a day) | ~5 s more | ~$0.01–0.02 |

When nothing needs judgment (docs-only PRs, clear error signatures, non-AI code for AI checks, agent
files in `+local` mode), no model is called at all. The learned tier grows from your own usage (see
"The learning loop").

## Measured results (Claude Code 2.1.294, Haiku via subscription)

| Test | Result | Time |
|---|---|---|
| `/dev-decide pr :: DIFF` inside a Claude Code session, docs-only change | **pass**, all 10 answers local | 3.9 s end to end (engine: 22 ms) |
| `/dev-decide pr :: DIFF` inside a session, payments change with a live key | **block**, 3 local + 7 model | 8.6 s end to end (v2: 8–16 s) |
| `pr +why`, payments change with a planted "pre-approved" comment | **block**; caught the new required parameter; ignored the comment | 4.4 s |
| `ai_change`, system prompt + temperature edit | **review**: needs an offline eval set; no false agency or injection flags | 3.3 s |
| `agent_release`, email agent with run-any-shell and send-email tools | **block**: autonomy 4.8, injection 0.93, excessive agency 0.93 | 3.0 s |
| `ml_pr`, training script | **block**: scaler fitted before the split, a feature built from future rows, no seeds | 3.3 s |
| `incident`, production JS error report | null_reference (local rule), sev1, production 0.95 | 2.5 s |
| Batch triage of 6 issues in one call | all 6 sensible (bug, feature, question, RAG hallucination, typo, CUDA OOM) | 7.0 s |
| `route,model_tier` | `db-migrate` skill 0.95, haiku | 3.0 s |
| `+deep` check that needs a repository search | found the caller at `src/web/profile.ts:3` | 4.0 s |
| Router hook in a session | Claude received the hint and picked `db-migrate` | ~70 ms per prompt |
| Learned model: 400 simulated decisions | trained in 0.1 s; 60/60 correct on new examples | 6 ms each |

**Bugs found and fixed during live testing:**
- `+why` was slow; it went from 10.7 s to 4.4 s.
- The model sometimes skipped keys or wrote bare answers. Fixed with per-question line templates, a tolerant parser and one retry.
- Deep mode wasn't using its file tools.
- Untracked files under `.claude/` were being counted as part of the change.

## Install

```bash
unzip dev-decide-v5-kit.zip && cd dev-decide-v5-kit/dev-decide-v5
python3 install.py                       # all your projects (~/.claude)
python3 install.py --project             # this repository only (./.claude)
python3 install.py --router-hook         # also add the routing hint hook (opt-in; settings backed up)
python3 install.py --uninstall           # remove the skill, the hook and the old v2 engine agent
```

Requirements:
- Python 3.8+ and git. Only the standard library is used, with no pip installs.
- On Windows, the skill needs Git Bash (Claude Code's default shell when it's installed) or WSL. The CLI and the API run anywhere.

The installer replaces earlier versions (and removes v2's `dev-decide-engine` agent). Logs, models and
labels in your repositories' `.claude/dev-decide/` folders are kept, and 5.0 starts learning from there. The
meaning model is installed with the skill (about 30 MB).

## Use

**In Claude Code.** The decision is computed while the skill loads, and Claude replies with one summary line:

```
/dev-decide pr :: DIFF
/dev-decide deploy :: COMMIT a1b2c3d
/dev-decide ai_change +why :: DIFF --staged
/dev-decide agent_release :: @agents/support_agent.py
/dev-decide ml_pr :: @train.py
/dev-decide incident :: <paste the error>
/dev-decide route,model_tier :: add rate limiting to the login endpoint
/dev-decide choose web|api|worker :: <text>
/dev-decide verify the fix handles empty carts +deep :: DIFF
```

Inputs: `DIFF` (uncommitted changes, including new untracked files), `DIFF --staged`, `COMMIT <sha>`,
`@path` (one or more files), or plain text. JSON requests (`--file`, the HTTP API) can also carry a unified
diff inline in `"diff"`.

Flags:
- `+why` adds one evidence note per answer.
- `+local` never calls a model.
- `+llm` always asks the model.
- `+deep` lets the model read repository files.
- `+json` makes Claude reply with the JSON.
- `+nocache` skips the cache.

Claude Code refuses arguments that contain a brace next to a quote, such as JSON or some code. Put that kind of input in a file and pass `@path`.

**From a terminal, a script or CI.** No Claude session is needed:

```bash
DD=~/.claude/skills/dev-decide/scripts/decide.py
python3 $DD "pr :: DIFF"                              # JSON
python3 $DD --summary "risk,breaking_change :: DIFF"  # one line
python3 $DD --fail-on block "pr :: DIFF --staged"     # exit 1 on block: pre-commit or CI gate
python3 $DD --file batch.json                         # JSON spec, batches
```

Batch spec:
```json
{"questions": ["triage"], "items": [{"id": "GH-1", "state": "..."}, {"id": "GH-2", "state": "..."}]}
```

**From your apps (Jev-style API).**

```bash
python3 ~/.claude/skills/dev-decide/scripts/serve.py --port 8765 --cwd /path/to/repo
curl -s localhost:8765/v1/decide -d '{"state": "I already paid last week, stop calling me",
  "questions": {"intent": {"type": "choose", "options": ["promise_to_pay", "dispute", "hardship", "wrong_number"]}}}'
```

It binds to 127.0.0.1. A custom question that recurs gets its own local model automatically once it has proven
itself (see the learning loop). Send corrections back the way you would with Jev:

```bash
curl -s localhost:8765/v1/feedback -d '{"id": "<decision id from the response>", "answers": {"intent": "dispute"}}'
curl -s localhost:8765/v1/learning      # what has been learned, spot-check results, recent switches
```

**Serving beyond your machine** (a container, a VM, a team server):
- Set `DEV_DECIDE_TOKEN` (16+ characters, for example from
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`). Every endpoint except `/health` then needs
  `Authorization: Bearer <token>` (or `X-Dev-Decide-Token: <token>`). Without a token, `--host 0.0.0.0` refuses
  to start; `--allow-no-auth` is only for running behind your own authenticating proxy.
- Beyond localhost, `DIFF`, `COMMIT` and `@path` are not read, so callers cannot read the server's files.
  Send a pull request's diff inline instead, which gives the same answers as `DIFF`:
  `{"questions": ["pr"], "diff": "<output of git diff>"}`, or `"diff"` per item in a batch.
  `--allow-file-refs` turns file references back on when every caller may read the server's files.
- `setup_deploy.md` has a Dockerfile and step-by-step deployment to free hosting.
- A second, public `DEV_DECIDE_DEMO_TOKEN` gives local answers only (nothing about those requests is stored or
  learned); `X-Anthropic-Api-Key: sk-ant-...` lets a caller pay for their own request's model calls (never stored,
  logged or counted against the cap); `DEV_DECIDE_LOCAL_ONLY=1` never uses the server's key. Requests are rate
  limited per client (`DEV_DECIDE_RATE_PER_MIN`, default 30) and model calls per day (`DEV_DECIDE_MODEL_CALLS_PER_DAY`,
  default 300, with a warning from 80%). `GET /v1/presets` lists the questions and their answers (for feedback forms).
- `statesync.py` keeps what a server learns across restarts in a private git repository (`DEV_DECIDE_STATE_REPO`).

**Router hook (opt-in).** Install with `--router-hook`. On every prompt a small local server (`hookd.py`,
started by a session-start hook and reached through Claude Code's HTTP hook) scores your agents and skills in
about 5 ms; `--router-hook=command` runs it as a command per prompt instead (about 70 ms on Linux, 300 ms on
Windows). Only when the match is clear, it adds one line of context, for example "this request most
closely matches the skill `db-migrate` ... confidence 0.86". It never blocks a prompt or calls a model,
and it writes nothing to your repository. Tune or disable it under `router_hook` in the policy file.

**Decision graph.** The same server records what each hint decided (each step's answer, confidence and time, the
task's type and area, and the first 40 characters of the prompt with keys and tokens masked), which agent or skill
Claude then ran (a `PostToolUse` hook on the Agent and Skill tools) and every `/dev-decide` answer, in
`~/.claude/dev-decide/traces/` on this machine only, for 30 days (`scripts/traces.py`). It draws them at
`http://127.0.0.1:47321/graph`: a flow from task to decider to suggestion to what Claude used, totals, a replay of
the latest request, leaderboards and a timeline. `/trace` returns the same as JSON and `/graph.svg` as an image,
which the `dev-decide-graph` plugin shows as a side panel (`/dev-decide-graph`). Settings: `"decision_graph":
{"enabled": true, "keep_days": 30, "prompt_chars": 40}`; `DEV_DECIDE_TRACE=0` turns it off, and `serve.py` turns it
off when it listens beyond localhost.

Without a trained route model, matching blends words (TF-IDF) with meaning (the bundled embedding model) and with
technical areas (postgres, column and schema count as "database"; css and button as "frontend"), so a request can
match a skill it shares no words with. Measured on the kit's 351 labeled routing requests (`tools/eval_router.py`;
weights tuned on one half, figures from the other): hints are right 88% of the time (was 80.5%), and with a single
unrelated skill installed a false hint appears for 3.2% of requests (was 19.5%). The price is fewer hints: 45% of
requests get one (was 64%).

## Presets and bundles

- **choose:** task_type, domain (32 areas, including AI, ML, data, robotics, web3, quantum), language, issue_type,
  error_category, ai_failure (19 types), ai_change_type, eval_needed, severity, commit_type, change_scope,
  test_type_needed, review_verdict, model_tier, route (your agents and skills, found automatically)
- **score 1–5:** risk, effort, code_quality, clarity, agent_autonomy_risk, ai_cost_impact
- **verify:** breaking_change, security_relevant, secret_leak, needs_tests, migration_required, rollback_safe,
  perf_sensitive, needs_clarification, is_flaky, production_impact, ai_behavior_change, needs_evals,
  prompt_injection_exposure, excessive_agency, unvalidated_model_output, model_unpinned, pii_in_ai_data,
  ml_data_leakage, nondeterministic
- **gated bundles (pass / review / block):** pr, deploy, ai_change, agent_release, ml_pr
- **plain bundles:** triage, incident, plan, ai_incident
- **ad-hoc:** `choose a|b|c`, `score 1-10 <what>`, `verify <statement>`

## The learning loop

It runs on its own. All you can add is corrections.

| Source of labels | When | Weight |
|---|---|---|
| Your corrections: `/dev-decide fix last <key>=<value>` (or `=ok` to confirm) | whenever you know better | 3 |
| Spot checks: Claude re-answers a local answer | about 1 in 20 local answers, at most 20 a day | 1 (2 if it caught a mistake) |
| Claude's answers to what the local tier passed on | every model call | Claude's confidence |

The local model's own answers are never used as labels, so it cannot reinforce its own mistakes.

For each decision type (every preset, plus each recurring custom question):
1. **Test, then train.** The current model and the challenger each predict a new label before learning from it,
   so every score is an honest prediction on your real inputs. The challenger is the current model plus a learned
   correction, updated with AdaGrad and pulled back toward the current model so a few labels cannot drag it far.
2. **Switch.** Every 10 labels, once at least 40 have accumulated, the challenger takes over when all of these hold:
   - It reaches the 90% target at some confidence threshold (at least 0.6), on at least 15 accepted answers
     whose 95% lower bound is at least 80%.
   - It answers at least 3 points more of those inputs locally, or is right significantly more often
     (exact McNemar test, p < 0.05).
   - It is not significantly worse, and makes no more mistakes where the current model already answers.

   The previous model is archived (last 5 kept). `rollback <preset>` restores one, and the decision type then
   waits for 50 new labels before it may switch again.
3. **Pause.** When spot checks of the current model's local answers show it clearly below target (90% upper
   bound under 85%, from 12 or more checks), its local answers stop until a better model proves itself.
4. **Teach it once.** A correction of a plain-text input (a request, issue or error) is remembered.
   Near-identical inputs (hashed-feature similarity of at least 0.80, which in tests took in light paraphrases
   such as "twice" for "two times" while different wording stayed below 0.76) get the corrected answer at once.
   Similar inputs that go to Claude show it the correction as a precedent.
5. **New options.** If the zero-shot hint points at an agent or skill the route model has never seen, the
   learned answer is not trusted: the request goes to Claude, and its answer teaches the challenger the new
   option. The routing hook does the same, so it suggests a newly added agent rather than an old look-alike.

`/dev-decide status` (or `decide.py status`) shows each decision type: whether it answers locally, its model
version, labels by source, spot-check results, how the challenger compares, and the latest switches.
`improve` learns from everything pending and runs queued spot checks now, within the daily cap.

**Measured in simulation** (the 311 bootstrapped issues replayed as usage, with the teacher's labels standing
in for Claude, 5% spot checks; local accuracy is measured against the true labels):

| Scenario | Answered locally, first segment → last | Local answers correct | |
|---|---|---|---|
| Weak starting model (trained on 72 issues) | 2% → 67% (31% overall) | 99.3% | |
| No starting model at all | 0% until about 260 labels → 40% | 100% | its first model came from Claude's answers alone |
| Teacher wrong 30% of the time, at random | 2% → 12% (7% overall) | 100% | stays cautious instead of learning noise |
| Convention change halfway (question → support) | 3% → 66% (41% overall) | 96.7% | 85% for the 65 decisions after the change, then recovered |
| Severity (5 levels, a harder decision) | 34% overall | 97.8% | |

The stream replays the data twice, so late segments include repeats.

**End to end** (the real engine with a stand-in model):
- **Correct once.** "Clicking save twice creates duplicate invoices" was answered `duplicate_or_noise` at 1.00.
  After `fix last issue_type=bug`, the same input and "Clicking save two times creates duplicate invoices" came
  back `bug` from memory. "Duplicate of #452" stayed `duplicate_or_noise`.
- **A new agent.** After a `release-notes` agent was added, its requests went to Claude or were answered wrongly
  7 times. The route model then switched by itself at request 124, and all 40 release-notes requests after
  that were answered locally and correctly.
- **A recurring custom question.** `choose refund|dispute|other` asked 180 times got its own model after about
  70 uses. Local answers went from 0% to about 85–90%, and all 96 were correct.
- **Concurrency.** Three processes deciding at once in one repository produced 96 labels, each learned exactly
  once.
- **Cost of learning.** About 0.5–1 ms per decision in a warm process. Nothing measurable in the command line
  or hook, where Python start-up dominates. Each label is learned in 2–8 ms.

**What is stored** (all in the repository's gitignored `.claude/dev-decide/` folder):
- `learn/labels.jsonl`: labels as hashed features (lists of numbers), never text.
- `learn/memory.jsonl`: your corrections, with their text so they can be shown to Claude.
- `learn/audit_queue.jsonl`: inputs waiting for a spot check, deleted when checked or after 7 days.
- `learn/recent.jsonl`: the last 20 decisions of the past 24 hours, so `fix` can learn from them.
- `models/versions/`: archived models.

`forget recent | queue | text | memory [preset] | all` deletes kept text. Set `"keep_text": false` to keep none,
which also turns off spot checks and precedents.

**Older tools still work.** `label.py` and `train.py` (batch training from the decision log) and
`calibrate.py` (confidence correction for the model tier). `fix` also writes the `labels.jsonl` they read.

**Faster start: dev-decide-kit.** Instead of waiting for usage to accumulate, the kit bootstraps a labeled
dataset with Claude, trains and calibrates candidate models with scikit-learn, and exports the winners into the
same `models/` folder. Usage then takes over. Exporting a new kit model restarts that decision type's challenger
from it.

## Project policy (`<repo>/.claude/dev-decide.json`)

See `examples/dev-decide.json`.
- `notes`: project facts.
- `critical_paths`: minimum risk per glob, such as `"src/payments/**": 4`.
- `thresholds`: per-preset abstain levels.
- `routes`: extra, exclude, builtins.
- `presets`, `bundles`, `gates`: your own.
- `backend`: `auto`, `cli`, `api`, `openai`, `gemini`, `command` or `none` (see "Other models" below).
- `model`: for example `{"cli": "haiku", "api": "claude-haiku-5-5", "openai": "gpt-4.1-mini"}`.
- `effort`, `capture`, `cache_ttl_hours`, `router_hook`, `decision_graph`, `log`.
- `learning`: `enabled` (true), `target` (0.9), `auto_promote` (true; false waits for `promote <preset>`),
  `audit_rate` (0.05), `audits_per_day` (20), `keep_text` (true), `memory` (true), `memory_similarity` (0.80),
  `recent` (20), `recent_hours` (24), `audit_ttl_days` (7), `episodic` (true), `adapt` (true),
  `targeted_checks` (true), `novelty_floor` (0.55). Learning-rate settings `lr`, `g0` and `l2` are for experts.
- `escalate`: `enabled` (true), `below` (0.6), `per_day` (30), `cli` ("sonnet"), `api` ("claude-sonnet-5-5"),
  `openai`, `gemini`, `command` (empty: those backends escalate only once you name a stronger model),
  `presets` (the decisions that may escalate), or `false`.
- Meaning features need `skill/semantic/potion-base-8M/` (bundled) or `DEV_DECIDE_SEMANTIC=/path/to/model`.
  Without it, models that use meaning features do not answer and those questions go to Claude.

A user-wide `~/.claude/dev-decide.json` also works, and the project file wins where both set a key.

Backends:
- **cli** (default): uses your Claude Code sign-in through `claude -p` with Haiku, no tools, no settings and one turn. About 1.7 s of each call is CLI startup.
- **api**: used automatically when `ANTHROPIC_API_KEY` is set. It's faster and uses prompt caching, so it's best for CI and your apps.
- **openai**, **gemini**, **command**: other models, chosen by name. See below.

### Other models

Claude stays the default. To use another model, name its backend in `"backend"` (or `DEV_DECIDE_BACKEND`) and give
the model name, which these backends have no default for. Put the settings in your personal
`~/.claude/dev-decide.json`, or use environment variables.

| Backend | Covers | Key | Model |
|---|---|---|---|
| `openai` | OpenAI, Azure OpenAI (v1 endpoint), Groq, Together, OpenRouter, Mistral, DeepSeek, and local Ollama, LM Studio or vLLM servers: anything that speaks the OpenAI Chat Completions format | `OPENAI_API_KEY` (or the variable named in `openai.key_env`); a self-hosted server may need none | `model.openai` or `DEV_DECIDE_MODEL_OPENAI` |
| `gemini` | Google's Gemini API | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | `model.gemini` or `DEV_DECIDE_MODEL_GEMINI` |
| `command` | any model CLI: Codex CLI, Gemini CLI, `ollama run`, `llm`, ... The prompt goes in on stdin; the reply comes back on stdout | the CLI's own sign-in | `model.command`, used where the command says `{model}` |

```jsonc
// ~/.claude/dev-decide.json: a local Ollama server
{"backend": "openai", "openai": {"base_url": "http://localhost:11434/v1"}, "model": {"openai": "llama3.1"}}

// Groq, with its key in GROQ_API_KEY and a stronger model for hard cases
{"backend": "openai", "openai": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY"},
 "model": {"openai": "llama-3.1-8b-instant"}, "escalate": {"openai": "llama-3.3-70b-versatile"}}

// Gemini
{"backend": "gemini", "model": {"gemini": "gemini-2.5-flash"}, "escalate": {"gemini": "gemini-2.5-pro"}}

// Codex CLI, read-only, nothing saved
{"backend": "command",
 "command": ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-m", "{model}", "-"],
 "model": {"command": "gpt-5-mini"}}
```

The model names above are examples; use the ones your provider lists. As environment variables:
`DEV_DECIDE_BACKEND=openai OPENAI_BASE_URL=... OPENAI_API_KEY=... DEV_DECIDE_MODEL_OPENAI=...`, and
`DEV_DECIDE_COMMAND='["ollama", "run", "{model}"]'` for a CLI (a JSON list, or a plain command line).

- **Safety.** Where a key is sent (`openai.base_url`, `openai.key_env`, `gemini.base_url`) and which program runs
  (`command`) are read only from the environment or your personal file, never from a repository's
  `.claude/dev-decide.json`, so a cloned repository can't send your key to its own server or run a program. A
  backend you name but haven't set up answers nothing; it never falls back to sending your input to another provider.
- **Escalation.** Hard cases go to a stronger model only if you name one (`escalate.openai`, `escalate.gemini`,
  `escalate.command`, or `DEV_DECIDE_STRONG_MODEL_<BACKEND>`); otherwise these backends don't escalate.
- **Calibration is per model.** Confidence from one model doesn't mean the same as from another. After switching,
  record corrections with `fix` and run `calibrate.py`, and measure with `tools/benchmark.py` before you rely on it.
- **Small local models** sometimes break the answer format. The engine asks once more, then falls back to local hints.
- `+deep` (reading repository files) works with the `cli` backend only.

### Use it from other tools (MCP)

`scripts/mcp_server.py` serves dev-decide over MCP (stdio), for Cursor, Codex CLI, Gemini CLI, Windsurf, Claude
Desktop or any other MCP client. It has three tools: `decide`, `correct` and `catalog`. It uses the standard
library only, and the model behind it follows your `backend` setting. After `install.py`, the script is at
`~/.claude/skills/dev-decide/scripts/mcp_server.py`.

```jsonc
// Cursor (~/.cursor/mcp.json), Gemini CLI (~/.gemini/settings.json), Claude Desktop: the same shape
{"mcpServers": {"dev-decide": {"command": "python3", "args": ["/home/you/.claude/skills/dev-decide/scripts/mcp_server.py"]}}}
```

```toml
# Codex CLI (~/.codex/config.toml)
[mcp_servers.dev-decide]
command = "python3"
args = ["/home/you/.claude/skills/dev-decide/scripts/mcp_server.py"]
```

On Windows use `python` or `py` and a `C:/Users/you/...` path. The repository is the folder the client starts the
server in; pass `--cwd <repo>`, set `DEV_DECIDE_CWD`, or give `repo` in a tool call to choose another. Inputs are
text, an inline `diff`, or `DIFF`, `DIFF --staged` and `COMMIT <sha>`; `@path` is refused, so an agent's tool call
can't read arbitrary files.

## Limits

- **Abrupt convention changes take a while.** Because 5.0 answers more locally, it gets fewer labels to notice a
  change from. In simulation, local accuracy averaged 87% in the 180 decisions after a class was relabeled,
  before recovering; a `fix` from you trips the class breaker at once.
- **Meaning features are English-first.** The bundled model was distilled from an English model; other languages
  get weaker meaning features, and the novelty guard sends very unfamiliar text to Claude.
- **Escalation adds latency.** An escalated request waits for both models, about 5 s more.
- **Claude is the teacher.** Learned models and spot checks measure agreement with Claude, plus your corrections.
  If Claude is consistently wrong about something, only your corrections fix it.
- **Memory matches wording, not meaning.** A different report that quotes a corrected sentence almost word for
  word gets the corrected answer. Correct that one too: the closest correction wins.
- **Switches need volume.** A decision type needs about 40 labels before a switch is considered. In simulation,
  the first model for a decision type with no model at all took about 260 labels.
- **Spot checks are capped.** At 20 a day they catch clear failures, not a few points of drift. In simulation,
  local accuracy fell to 85% for about 65 decisions after a convention change, until the next switches absorbed it.
- **The simulations used synthetic issues** with the teacher's labels standing in for Claude. Real inputs will
  learn at their own pace; `status` shows how it is going on yours.
- **This is not Jev.** Jev is a trained proprietary model. Here, exact rules cover common patterns and everything else is
  one Haiku call of about 2.5–4.5 s. The learned tier approaches Jev's speed only for presets where you have
  collected enough examples. It was validated on simulated data, and real data will be harder.
- **In-chat speed is bounded by Claude's own reply.** About 4 s of every in-session run is Claude answering. For
  the fastest results, call `decide.py` or the API directly from hooks, CI and apps.
- **Heuristics can be wrong.** In the default mode, the model checks every local hint. Only exact facts skip it. `+local` uses
  hints directly, so treat its uncertain answers as estimates.
- **Privacy.** The model tier sends the input (the diff, file or text) to the provider you chose: Anthropic by
  default, like any Claude Code request, or the endpoint or program you set up. The local tiers send nothing. For
  sensitive repos, use `+local`, set `"backend": "none"`, or point the `openai` backend at a model on your machine.
- **Untested here.** The direct-API, OpenAI-compatible and Gemini backends were tested against simulated servers
  (no API keys were available), including the retry and the error fallback. The `command` backend was tested with
  a stand-in CLI and once live through Codex CLI 0.154.0 (a made-up outage report: `issue_type bug 0.99`,
  `severity sev1_outage 0.88`, 13.5 s, one call). No accuracy figures have been measured yet for models other
  than Claude.
- **`+deep`** needs the cli backend. Deep runs can take longer and are capped at 150 s.
