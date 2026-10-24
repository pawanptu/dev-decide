# dev-decide 5.0.1 + dev-decide-kit

dev-decide is a Jev-style decision engine for software and AI work: a Claude Code skill, a command-line tool,
an HTTP API and an opt-in routing hook. It answers typed questions (route this request, triage this issue, rate
this diff) locally in milliseconds when it can, asks Claude when it can't, and learns from every answer.

## Install (as a Claude Code plugin)

In Claude Code:

```
/plugin marketplace add pawanptu/dev-decide
/plugin install dev-decide@dev-decide
```

Then use it in any repository, for example `/dev-decide pr :: DIFF` or `/dev-decide triage :: <paste an issue>`.

- **Your own key.** Nothing here uses anyone else's credentials. By default it asks Claude through your own Claude
  Code sign-in. To use the API instead, set `ANTHROPIC_API_KEY` in your shell. Never commit the key.
- **Needs** Python 3.8+ and git on your PATH as `python3` (on Windows with only `py`, use the clone install below),
  and Git Bash or WSL on Windows.
- **Starter models are built in.** The first time it runs in a repository it copies in pretrained models
  (`skill/starter-models/`, 2.6 MB), so `issue_type` is answered locally from day one. The other models switch on by
  themselves once they have learned enough from your use. A repository that already has models is never overwritten.
- **Routing hint hook is included.** On each prompt it checks, locally and in about 70 ms, whether one of your agents or
  skills clearly matches, and only then adds a one-line hint. It never calls a model and never blocks a prompt. To
  turn it off, put `{"router_hook": {"enabled": false}}` in `.claude/dev-decide.json`, or disable the plugin's hooks.

**Install from a clone instead** (also adds the optional routing hook, and works when only `py` exists on Windows):

```
git clone https://github.com/pawanptu/dev-decide
python3 dev-decide/dev-decide-v5/install.py              # add --router-hook for the routing hint
```

**Maintainers:** the engine lives in `dev-decide-v5/skill/`. After changing it, run `python3 tools/sync.py` to copy it
into `plugin/` and `dev-decide-server/`.

## What is in this repo

- **`dev-decide-v5/`**: the engine, with its bundled meaning model (30 MB, MIT). Install it in place of 3.x or
  4.x; your logs, models and labels are kept.
- **`dev-decide-kit/`**: optional. It bootstraps labeled data with Claude and trains starting models. It ships
  with pretrained models from a real run (821 examples, 89 Claude calls, $1.22), retrained for 5.0.
- **`setup_deploy.md`**: step-by-step setup on your computer, and deployment of the HTTP API on free hosting.

**New in 5.0.1: the HTTP API can be deployed.** It checks a token (`DEV_DECIDE_TOKEN`), refuses to listen beyond
localhost without one, never reads the server's own files for a remote caller, and takes a pull request's diff
inline (`"diff"`). Decisions are otherwise the same as 5.0. Don't put the 5.0 API on the internet: it has none of
these checks.

## What 5.0 adds

| | What it does | Measured |
|---|---|---|
| Understands meaning | 256 meaning features per input from a small embedding model, used where they measure better | routing 84.9% → 90.6% held out; domain 57.9% → 68.4% |
| Learns from each answer | Recalls inputs like ones already labeled, trusted only where its record reaches 90% | steady usage: 80% answered locally at 100% (4.0: 60%) |
| Tunes itself | Per-class thresholds follow spot checks; checks target borderline, unfamiliar and recalled answers; a class breaker; unfamiliar inputs go to Claude | with a convention change: 79% local at 96.6% (4.0: 57% at 97.8%) |
| Stronger model for hard cases | Unsure answers to decisions that matter are re-asked to Sonnet, at most 30 a day | live: Haiku 0.60 → Sonnet agreed at 0.62, $0.016, 7.6 s |

From 4.0, it still learns from Claude's answers, spot checks and your corrections, switches models only on
evidence, and can roll back. Nothing runs in the background and no code changes itself. With learning and
escalation off it answers exactly like 3.1 (99 of 99 test decisions).

**Speed.** Routing through the local HTTP API takes 4.1 ms and triage 3.2 ms: 17–150× faster than Jev's reported
70–500 ms. The command line and routing hook take 72–74 ms, mostly Python start-up.

**Honest limits.** Local answers agree with Claude plus your corrections, no more. After an abrupt change in how
a class should be labeled, 5.0 dips further than 4.0 for a while (87% vs 92% right over the next 180 decisions in
simulation), because more local answers mean fewer labels to notice the change from; one `fix` from you speeds
that up. Meaning features are English-first. The simulations used synthetic issues.

## Quick start

```bash
cd dev-decide-v5 && python3 install.py && cd ..          # 1. install (or --project for one repository)
pip install numpy scipy scikit-learn                     # 2. optional, for the kit: pretrained starting models
python3 dev-decide-kit/kit.py export --data dev-decide-kit/pretrained --repo /path/to/repo
```

`setup_deploy.md` has the full steps, including deployment. Then use it. In Claude Code: `/dev-decide route :: <task>`, `/dev-decide triage :: <issue>`,
`/dev-decide fix last issue_type=bug`, `/dev-decide status`. `dev-decide-v5/README.md` has the details
("5.0 measurements", "The learning loop"); `dev-decide-kit/README.md` covers training your own.

## Licence

MIT. See `LICENSE`. The bundled meaning model (potion-base-8M) is MIT licensed too; see its `NOTICE`.
