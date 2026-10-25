# Contributing to dev-decide

Thanks for helping. dev-decide is MIT licensed and open to contributions: code, bug reports, labeled examples, docs and ideas.

## Where things live

| Folder | What it is |
|---|---|
| `dev-decide-v5/skill/` | The engine. This is the source of truth. |
| `plugin/` | The Claude Code plugin. Its `skills/dev-decide/` is a copy of the engine, made by `tools/sync.py`. |
| `plugin-graph/` | The `dev-decide-graph` side panel: a Claude Code plugin with a TypeScript hooks module. |
| `dev-decide-kit/` | Optional kit: bootstraps labeled data and trains starting models. |
| `tools/` | Smoke tests, the benchmark and evaluation scripts. |

Edit the engine in `dev-decide-v5/skill/`, then run `python3 tools/sync.py` so the plugin copy matches. Do not edit the copy in `plugin/skills/dev-decide/` by hand.

## Set up

You need Python 3.8+ and git. The engine uses only the standard library. The kit needs `pip install numpy scipy scikit-learn`.

```bash
git clone https://github.com/pawanptu/dev-decide.git
cd dev-decide
python3 tools/smoke.py          # fast checks, no model calls
```

On Windows, the skill needs Git Bash or WSL.

## Before you open a pull request

CI runs on Ubuntu and Windows with Python 3.8 and 3.12. Run the same checks locally:

```bash
python3 -m compileall -q dev-decide-v5/skill/scripts dev-decide-kit tools
python3 tools/smoke.py
python3 tools/sync.py           # if you changed the engine
claude plugin validate plugin   # if you changed the plugin
claude plugin validate plugin-graph && claude plugin test plugin-graph   # if you changed the side panel
```

The side panel uses Claude Code's hooks modules, so validate and test it with Claude Code 2.1.310 or later; an older
`claude` reports errors that are not there.

## The web pages

- **The web console** (`dev-decide-v5/skill/ui.html` and `ui.js`) runs on Alpine.js's CSP build, vendored in
  `skill/vendor/` (see its README for the version and checksums). The page's CSP is `script-src 'self'`, so put
  logic in `ui.js` (an `Alpine.data` component), never in an inline `<script>`. Templates can only read properties
  and call methods: no arrow functions, template literals or globals such as `Math` or `JSON` in an attribute.
- **The decision graph page** (`skill/graph.html`) is one self-contained file served by `hookd.py`. Its colors are
  validated for color-blind readers in both themes; keep text in the ink colors, never a series color, and keep
  every animation off under `prefers-reduced-motion`.
- Check both at phone width (390 px) with no sideways scroll, and in light and dark.

Please also:
- Keep changes small and focused. One topic per pull request.
- Add or update a smoke test when you fix a bug.
- Update `CHANGELOG.md` for any change a user would notice.
- Never commit secrets, `.env` files or `.vscode/`.

## Changing decisions or models

Decision quality is measured, so changes to scoring, thresholds or trained models need evidence:

```bash
python3 tools/benchmark.py      # trains and evaluates on the shipped data, no model calls
```

Include the before and after numbers in the pull request. A change that makes held-out accuracy or calibration worse will not be merged unless it fixes a documented bug.

## Contributing labeled data

New labeled examples help every user. Real, anonymized examples are best. Do not include private code, customer data, secrets or personal information. By contributing data you agree it is released under the MIT license.

## Reporting bugs and asking for features

Open a GitHub issue and use the template. Include your OS, Python version, the command you ran, and the output. For anything that looks like a security problem, do not open a public issue: see [SECURITY.md](SECURITY.md).

## Code style

Match the surrounding code: naming, comment density and idiom. The engine avoids third-party dependencies on purpose.

## License

By contributing, you agree your contribution is licensed under the [MIT License](LICENSE).

Everyone taking part is expected to follow the [Code of Conduct](CODE_OF_CONDUCT.md).
