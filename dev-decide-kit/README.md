# dev-decide-kit

Trains fast, calibrated CPU models for dev-decide's learned tier, so that more decisions are answered
locally in well under a millisecond instead of by a multi-second model call.

It bootstraps a labeled dataset with Claude (Haiku writes diverse examples, Sonnet labels them blind),
trains up to 40 candidate models per decision with scikit-learn (word features, and with dev-decide 5's bundled
embedding model also word-plus-meaning features), calibrates them, picks winners by calibration, then accuracy,
then speed, and exports them into dev-decide's `models/` folder. A model only
auto-answers above the confidence threshold it proved on held-out data; everything else still falls
through to the LLM tier.

Decisions covered: `route` (your agents and skills), `risk` and `breaking_change` (pull requests), and
`issue_type`, `severity` and `domain` (issue triage).

## Measured results (live run, 19 October 2026; retrained with meaning features for dev-decide 5)

Bootstrap: 821 examples (360 routing requests, 150 diffs, 311 issues), written by Haiku and labeled blind
by Sonnet, in 89 model calls for $1.22. Retraining with meaning features needed no new calls and took about
10 minutes on 2 CPU cores.

| Decision | Features chosen | Answered locally (test) | Overall test accuracy (was) | Status |
|---|---|---|---|---|
| `route` | words + meaning (f3), retrained 25 October with chat | 85% at 95% accuracy | 87.7% on a test split that now includes chat | **enabled** |
| `issue_type` | words + spelling (f2), retrained 25 October | 93% at 96% accuracy | 95.3% | **enabled** |
| `severity` | words (f1), retrained 25 October | 45% at 98% accuracy | 73.2% | **enabled** |
| `domain` | words + meaning (f4), retrained 25 October | 70% at 92% accuracy | 80.2% across 32 classes | **enabled** |
| `breaking_change` | words (f1), retrained 25 October | 57% at 91% accuracy | 76.0% | **enabled** |
| `risk` | words + spelling (f2) | 9%, 2 answers | 68.2% (63.6%), 82% within one level | not yet |

Meaning features won where the decision is about topic (routing, domain), and word features where it is about
the kind of report (severity), so each decision keeps what measures best.

**Retrained `issue_type` (25 October 2026): short reports.** Every generated issue except the noise ones was 15 words
or longer, so the first model learned "short means noise": it called "App crashes on startup" and "Please add dark
mode" `duplicate_or_noise` at about 100%, and answered them locally. 150 short, hand-labeled reports (all six types,
`data/triage/short_examples.py`) were added and the model retrained with no model calls; meaning features (f3) now
win. On 60 short reports it never trained on (`data/triage/short_test.jsonl`, `tools/eval_issue_type.py`):

| | First model | Retrained |
|---|---|---|
| Accuracy on short reports | 35.0% | 88.3% |
| Answered locally / right when answered | 96.7% / 34.5% | 91.7% / 94.5% |
| Long reports held out by both runs (40) | 95.0% | 95.0% |

Repositories that still hold an earlier starter model get the new one on their next decision; models you trained
or the learning loop promoted are never replaced.

**Second retrain (25 October 2026, dev-decide 5.1): 772 more issues and per-class thresholds.** Haiku wrote 800 new
issues and Sonnet labeled them blind (agreeing with the writer on 537 of 625 issue types; disagreements are not
trained on), in 155 calls. The 150 hand-labeled short reports were checked blind by Sonnet too: it agreed on 205 of
206 and the one it disputed was removed. Training now also measures, for each answer class, the confidence its
answers need to reach 90% on out-of-fold predictions, so weak classes need more before they are answered locally
(for example `severity` never answers sev2_major or sev3_minor locally). All three triage models are now enabled.
On examples that neither the earlier run nor this one trained on:

| | Accuracy (was) | Answered locally (was) | Right when answered (was) |
|---|---|---|---|
| `issue_type`, 39 long reports | 97.4% (94.9%) | 94.9% (97.4%) | 97.3% (97.4%) |
| `issue_type`, 60 short reports | 88.3% (88.3%) | 83.3% (91.7%) | 96.0% (94.5%) |
| `domain`, 28 long reports | 82.1% (67.9%) | 67.9% (46.4%, but not enabled) | 100% (84.6%) |
| `severity`, 38 long reports | 76.3% (84.2%) | 47.4% (71.1%, but not enabled) | 94.4% (85.2%) |

`severity` is right less often overall on these 38, but answers only where it is reliable; before, it was not
enabled at all. The run's report is `reports/train-2026-10-25-triage-5.1.json`. The first attempt at 500 new diffs hit the
Claude plan's session limit (the kit now treats such replies as failures and never caches them); a second run wrote
497 (147 calls, about $7 of plan usage, roughly $0.014 per diff). `breaking_change`, retrained on 647 diffs, is now
enabled; on the 18 diffs neither run trained on, accuracy went from 66.7% to 72.2% and its local answers from 71.4%
to 85.7% right (`reports/train-2026-10-25-pr-5.1.json`). The retrained `risk` model measured worse on its 19 shared
held-out diffs (52.6% against 63.2%) and still reached no reliable threshold, so the original `risk` model stays.

**Route model retrained with chat (25 October 2026).** Its "no agent" examples were all non-software tasks, so it sent
development-flavored chat to an agent ("where do I find this setting" went to security-auditor at 0.91). 77
hand-written chat and meta messages labeled "none" (`data/route/chat_examples.py`) were added and the model retrained,
with no model calls. On 42 differently-worded chat messages it never trained on (`data/route/chat_test.jsonl`), wrong
hints fell from 24 to 1. On 51 real requests neither run trained on, it hints on 71% (was 82%) at 94.4% right (was
92.9%). Both chat sets were written by one person, so real chat may score a little lower.

**Speed.** Measured on a 2-core cloud VM with no model call, against Jev's reported 70–500 ms:

| How dev-decide is called | Median per decision | Compared with Jev |
|---|---|---|
| The model alone (featurize + predict) | 0.14 ms | - |
| dev-decide's analyzers + the model, in-process (the report's figure) | 0.68 ms | 103–733× faster |
| A whole request in a warm process (parsing, cache, decision log) | 1.7–2.0 ms | 35–300× faster |
| The local HTTP API (`serve.py`, keep-alive connection) | 2.8–3.1 ms | 23–180× faster |
| The same request again (cache hit), through the API | 1.4–1.5 ms | 47–360× faster |
| A pull-request bundle (10 questions and a gate, diff read from git), warm process | about 11 ms | 6–45× faster |
| A new process per call: the router hook | about 55 ms | 1.3–9× faster |
| A new process per call: the `decide.py` command line | about 70–80 ms | about the same as Jev's fastest |

The first rows are routing and triage, the two decisions the trained models now answer. A warm process beats
Jev's reported latency everywhere it was measured. The command line and the hook only match its best case:
starting Python and compiling the analyzers' patterns takes about 45 ms before any work begins. One Claude call
(the LLM tier) takes 2.5–4.5 s. dev-decide 3.1 keeps loaded models, the agent and skill list and the git root in
memory between requests, which took a routing request through the HTTP API from about 11 ms to about 3 ms with
identical answers (99 of 99 test decisions matched).

**End to end.** In a real Claude Code session,
`/dev-decide route :: add a discount_code column to invoices and make sure the migration can be rolled back`
was answered by the learned tier (`db-migrate`, 18 ms of engine time, no model call). A low-confidence issue
fell through to Claude and came back correct (`feature_request`) in 2.4 s.

The four decisions marked "not yet" keep using dev-decide's rules and LLM tier exactly as before, so they lose
nothing. They need more data, and for PR risk, real diffs (see "Making it more accurate"). Full details are in
`pretrained/reports/report.md`.

**After export, dev-decide keeps learning.** The kit gives each decision type a starting model;
from then on Claude's answers, spot checks and your corrections improve it, and dev-decide switches models only
when a candidate proves better on your own decisions (see "The learning loop" in `dev-decide-v5/README.md`).
The four decision types that are "not yet" enabled can get there through use.

**A spot check on hand-written inputs is less rosy than the test split.** On 12 issues written by hand, outside
the bootstrap, `issue_type` answered 10 locally and got 8 right; it was confidently wrong twice ("Clicking save
twice creates duplicate invoices" → `duplicate_or_noise` at 1.00, because of the word *duplicate*, and "wrong
currency symbol" → `docs` at 0.83). On 12 hand-written routing requests, `route` answered 11, all correctly.
The 98% test figure is agreement with the teacher on synthetic issues; real issue wording differs, so label a
sample of your own issues before relying on automatic triage.

## What's in the repository

| Folder | What it is |
|---|---|
| `dev-decide-v5/` | The dev-decide engine, which reads the kit's models and then keeps learning from use. Install this; it replaces 3.x and 4.x. |
| `dev-decide-kit/` | This kit. |
| `dev-decide-kit/pretrained/` | Data, models and the report from the run above, so you can use the triage and PR models today. |

## Quick start

```bash
# 1. dev-decide (reads the kit's models, then keeps improving them from use)
cd dev-decide-v5 && python3 install.py && cd ..

# 2. training dependencies (dev-decide itself stays standard-library only)
pip install numpy scipy scikit-learn

# 3a. use the pretrained triage and PR models in a repository right away
python3 dev-decide-kit/kit.py export --data dev-decide-kit/pretrained --repo /path/to/repo \
    --only risk,breaking_change,issue_type,severity,domain

# 3b. or train your own (recommended for route, which must match your agents and skills)
cd /path/to/repo
python3 /path/to/dev-decide-kit/kit.py plan        # what it will generate and how many model calls
python3 /path/to/dev-decide-kit/kit.py generate    # asks before spending; Ctrl-C safe, answers are cached
python3 /path/to/dev-decide-kit/kit.py train       # about a minute on a laptop CPU
python3 /path/to/dev-decide-kit/kit.py export --repo .
```

`--routes auto` (the default) reads the agents and skills dev-decide sees in the current repository and
`~/.claude`. The pretrained `route` model was trained on an example catalog (`pretrained/routes.json`), so
it is only useful if your agents and skills match it.

## How it works

**Bootstrap.** For each decision the writer model (Haiku) produces examples aimed at chosen targets:
requests for each route plus near misses, issues spread evenly over types and domains, and git diffs
spread over risk levels, stacks and kinds of change. A separate labeler (Sonnet) then labels every
example blind, in shuffled batches, without seeing the target. When the two disagree (beyond one step for
ordered labels such as risk), the example is kept for statistics but not used for training. Exact
duplicates are skipped, and every answer is cached, so re-runs and crashes never pay twice.

**Your real data.** `generate --from-file requests.txt --family route|triage` labels your own requests or
issues; `generate --from-git /path/to/repo --pr-n 300` labels real commits for PR risk. Real inputs beat
synthetic ones for distribution.

**Honest splits.** Near-duplicates (MinHash over word shingles) are grouped and always land in the same split
and fold. A test split (15%, stratified by label at the group level) is set aside first and never used for
any choice. Human-reviewed examples (`kit.py review`) become the test set when there are at least 20.

**Candidates.** Up to four featurizers (`f1`: words, bigrams, file names and dev-decide's analyzer signals; `f2`:
`f1` plus character 3- and 4-grams; `f3` and `f4`: `f2` plus a 256-number sentence embedding at half or full
weight, when dev-decide 5's embedding model is present), five L2 strengths, with and without class balancing:
up to 40 per decision. Set `KIT_NO_SEMANTIC=1` to train word features only.
Each is scored by 5-fold cross-validation on the development set and calibrated by temperature scaling on
its out-of-fold predictions. Cross-validation matters with bootstrap-sized data: an early version that used
a small calibration split picked a near-constant model for `breaking_change`, because sampling noise made
the better models look miscalibrated.

**Selection** (your three priorities, in order):
1. calibration is a constraint: out-of-fold ECE may exceed the *noise floor* (the ECE a perfectly calibrated
   model would show at that sample size, estimated by simulation) by at most 0.05
2. accuracy is the objective: highest cross-validated accuracy, with candidates within one standard error tied
3. latency breaks ties: among tied candidates, the fastest featurizer, then the lowest log-loss
If nothing passes the calibration test, the lowest log-loss wins, so a constant model can never win.
The winner is then refit on the whole development set.

**Auto-accept threshold.** The lowest confidence at which cross-validated accepted answers reach the target
accuracy (default 90%), but never below 0.6 (`--min-confidence`), the level below which dev-decide marks an
answer as abstain; those inputs go to the LLM tier instead. A model is enabled only if, on the test split, at
least 10 answers clear that threshold, their accuracy is within 5 points of the target, and its 95% lower
bound is within 15 points.

**Export.** The winner is pruned and stored as float32 in dev-decide's binary format. Every test number in
the report comes from that exported model running through dev-decide's own predict code, and export checks
that the files dev-decide loads reproduce the kit's probabilities.

## Reading the report

`reports/report.md` (in the data folder) shows, per decision:
- **Accuracy with a 95% interval.** Agreement with the teacher's labels on held-out examples, or with people
  when a gold set exists. Small test sets give wide intervals; the report says so.
- **ECE** (expected calibration error) before and after calibration: how far confidence is from accuracy.
- **AURC** (area under the risk-coverage curve): lower means mistakes sit at low confidence, where they fall
  through to the LLM.
- **Auto-accept**: the share of inputs answered locally and how often those answers are right.
- **Ordinal MAE / within one level** for risk and severity, **AUROC** for breaking_change.
- **Latency**: model only, and the model with dev-decide's analyzers, in-process.

## Making it more accurate

- More data: `--route-per-class 80 --triage-n 800 --pr-n 400`. Re-running `generate` with a new `--seed` adds
  fresh examples; previous answers stay cached.
- Real data via `--from-file` and `--from-git`.
- A human gold set: `kit.py review export --family triage --n 60`, fill in the CSV, `kit.py review apply`.
- A stronger labeler: `--label-model opus` (costs more per call).
- `domain` has 32 classes, so it needs roughly 30 or more examples per domain before it can be enabled.

## Commands

```
kit.py plan | generate | review export|apply | train | report | export | all
  --data DIR               working folder (default ./dev-decide-kit-data)
  --families route,pr,triage
  --routes auto|FILE       agents and skills to route between
  --route-per-class N  --triage-n N  --pr-n N
  --gen-model haiku  --label-model sonnet  --backend auto|cli|api|mock
  --budget N               hard cap on model calls (default: the plan + 15%)
  --workers N  --seed N  --yes
  --from-file FILE --family route|triage    --from-git REPO
  --target 0.9  --min-coverage 0.1  --ece-max 0.05  --min-n 40
  --repo PATH  --only TASKS  --force
```

The teacher uses your Claude Code sign-in (`claude -p`), or the API when `ANTHROPIC_API_KEY` is set.
`--backend mock` runs everything offline with a fake teacher for testing.

## Limits

- **Accuracy is agreement with the teacher** unless you build a human gold set, so a student can match but
  not exceed its teacher's judgment on the teacher's own labels.
- **Synthetic data is not your data.** The report's numbers are on bootstrapped examples; label real ones
  to see how they transfer. A hand-written spot check of `issue_type` scored 8 of 10 against 98% on the test
  split (see above), and `kit.py export` prints a reminder while no human gold set exists.
- **`route` must be retrained** whenever you add, rename or remove agents or skills.
- **Speed depends on how dev-decide is called.** The millisecond figures need a warm process (the HTTP API or
  your own long-running code). The command line and the router hook start Python for every call, about
  55–80 ms, which only matches Jev's best case.
- **This is not Jev.** Jev is one general model behind an API. These are narrow models, one per decision,
  that beat it on latency because they run locally; their accuracy is only comparable within this report.
