"""Short issue reports, hand-labeled, to fix the starter issue_type model's length bias.

The generated triage examples are all 15+ words except for duplicate_or_noise, so the first model learned
"short means noise" and called "App crashes on startup" noise at 100%. TRAIN rows are appended to
examples.jsonl (source "hand-labeled-short"); TEST rows are a separate held-out set, phrased differently,
that is never trained on (tools/eval_issue_type.py reads it). Labels follow ISSUE_TYPES in kitlib/prompts.py.

  python3 dev-decide-kit/pretrained/data/triage/short_examples.py   # rewrites the hand-labeled rows
"""
import hashlib
import json
from pathlib import Path

TRAIN = {
    "bug": [
        "Login returns 500", "App crashes on startup", "Checkout broken", "Search is slow",
        "Dashboard shows wrong totals", "CSV export is empty", "Dark mode toggle does nothing",
        "Upload fails for files over 10MB", "Training loss is NaN after epoch 2", "Webhook never fires",
        "Pagination skips the last page", "Images not loading on Safari", "Memory leak in the worker",
        "Infinite spinner on settings page", "API returns stale data after update",
        "Timezone off by one hour in reports", "Build fails on Windows", "Segfault when loading the model",
        "Tooltip renders behind the modal", "Double charge when clicking pay twice", "Sort by date is reversed",
        "App freezes when switching tabs", "Notifications sent twice", "Mobile menu won't close",
        "Cron job runs every minute instead of hourly", "Embeddings are all zeros",
        "Cannot save draft, nothing happens", "Autocomplete drops the first letter",
        "CUDA out of memory with batch size 8", "Logout doesn't clear the session",
        "Chart legend overlaps the axis", "Retry loop never stops", "Unicode names get mangled",
        "Websocket disconnects every 30 seconds", "Wrong currency symbol for EUR",
    ],
    "feature_request": [
        "Please add dark mode", "Add CSV export", "Support for SSO login", "Would love keyboard shortcuts",
        "Add an option to mute notifications", "Support Python 3.13", "Allow bulk delete",
        "Add a dark theme for the editor", "Can we get a REST endpoint for invoices",
        "Make the sidebar collapsible", "Add retry with backoff to the client", "Support streaming responses",
        "Export charts as PNG", "Add German translation", "Let admins set rate limits per team",
        "Add a --dry-run flag", "Support webhooks for new orders", "Two-factor authentication please",
        "Add filtering by tag", "Option to disable telemetry", "Add GPU support for inference",
        "Allow custom stop sequences", "Add a search bar to settings", "Support Postgres as a backend",
        "Feature: schedule reports weekly",
    ],
    "question": [
        "How do I reset my API key?", "How do I enable debug logging?", "Is there a way to export to Excel?",
        "Why is the first request so slow?", "Does this support ARM Macs?", "What's the default timeout?",
        "How do I run tests locally?", "Can I self-host this?", "Which Python versions are supported?",
        "How to configure a proxy?", "Why does the model output change between runs?", "Is a GPU required?",
        "Where are the logs stored?", "How can I change the batch size?", "Do you support SAML?",
        "What does error E1042 mean?", "Is the free tier rate limited?", "How do I upgrade from v2 to v3?",
        "Can I use this with Docker Compose?", "How do embeddings get cached?",
        "why is my build cache never hit?", "what license is this under?",
        "How do I add a custom tool to the agent?", "Any way to run this offline?", "How do I set the region?",
    ],
    "support": [
        "I can't log in to my account", "Charged twice this month", "Please refund my order",
        "My account is locked", "Need to change the email on my account", "Cancel my subscription please",
        "Invoice shows the wrong company name", "Lost access to my 2FA device", "Where is my order #4412?",
        "Please delete my account", "Upgrade didn't apply to my plan",
        "Can't access the billing page for our team", "My trial ended early",
        "Transfer my workspace to a new owner", "Promo code not applied to my order",
        "Reset my password, the link expired", "Our seat count is wrong on the invoice",
        "Please restore my deleted project", "I was billed after cancelling", "Account suspended without notice",
    ],
    "docs": [
        "Typo in the README", "Docs for the CLI are outdated", "Broken link in the install guide",
        "Missing docs for the batch API", "Example in quickstart doesn't compile", "README still mentions v1",
        "Docstring for fit() is wrong", "API reference missing the timeout param",
        "Clarify the rate limit section in docs", "Changelog missing 2.3 release notes",
        "Tutorial skips the auth step", "Docs say Python 3.7 but 3.9 is required",
        "Add an example for streaming to the docs", "Wrong default value in the config docs",
        "Migration guide is incomplete", "Screenshot in docs is outdated", "Contributing guide link is dead",
        "Typo: recieve -> receive in docs", "Docs don't explain env vars", "FAQ answer about pricing is outdated",
    ],
    "duplicate_or_noise": [
        "+1", "same here", "me too", "bump", "any update?", "test", "asdf", "duplicate of #45",
        "dup of #2291", "same issue", "+1 please", "following", "same problem on mac", "still happening +1",
        "closing, duplicate", "ignore this", "test issue please ignore", "++", "\U0001F44D", "same as above",
        "duplicate", "any news on this?", "this", ".", "hello",
    ],
}

TEST = {
    "bug": [
        "Signup button does nothing", "Reports page times out", "Crash when opening settings",
        "Totals are off by a cent", "Email preview is blank", "Model predicts the same class for everything",
        "Can't scroll on iPad", "Export hangs at 99%", "Sync deletes local changes", "Error 502 on upload",
        "Graph doesn't update after filtering", "Wrong language after login", "Orders page query is slow",
        "Push notifications stopped working",
    ],
    "feature_request": [
        "Add Slack integration", "Support YAML config", "Please add an undo button", "Dark mode for mobile",
        "Allow exporting to JSON", "Add pagination to the API", "Support Windows on ARM",
        "Add a CLI flag for verbose output", "Webhook retries would be nice", "Let users pick a theme",
    ],
    "question": [
        "How do I change my username?", "Does it work with Node 22?", "What is the max file size?",
        "How to enable caching?", "Why does the cache expire so fast?", "Is there an API for this?",
        "How do I install on Linux?", "Can I run it on a Raspberry Pi?", "Where do I find the API docs?",
        "What models are supported?",
    ],
    "support": [
        "Refund my last payment please", "Can't get into my account since yesterday", "Change the billing address on my invoice",
        "Stop my plan from renewing", "I was charged for a seat I removed", "Please merge my two accounts",
        "My order never arrived", "Need an invoice for March", "Restore access to my team workspace",
    ],
    "docs": [
        "Fix typo on the pricing docs page", "Install docs are wrong for macOS",
        "Docs missing an example for webhooks", "README links are broken", "Outdated screenshots in the guide",
        "Document the --force flag", "API docs list a removed endpoint", "Spelling mistake in the tutorial",
    ],
    "duplicate_or_noise": [
        "+1 same", "Same thing happening to me", "Bumping this", "dupe of #77", "testing 123", "Happening to us as well",
        "any update on this", "qwerty", "Duplicate of the login issue",
    ],
}

SOURCE = "hand-labeled-short"


def row(text, label):
    key = hashlib.sha1(("short|" + text).encode("utf-8")).hexdigest()[:16]
    return {"id": "triage-" + key[:12], "key": key, "family": "triage", "source": SOURCE,
            "input": {"kind": "text", "text": text}, "labels": {"issue_type": label},
            "teacher": {"issue_type": [label, 0.9]}, "agree": {"issue_type": True},
            "meta": {"labeled_by": "hand", "note": "short reports, to fix the length bias"}}


def main():
    here = Path(__file__).resolve().parent
    examples = here / "examples.jsonl"
    kept = [l for l in examples.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("source") != SOURCE]
    added = [json.dumps(row(t, y), ensure_ascii=False) for y, ts in TRAIN.items() for t in ts]
    examples.write_text("\n".join(kept + added) + "\n", encoding="utf-8")
    test = [json.dumps(row(t, y), ensure_ascii=False) for y, ts in TEST.items() for t in ts]
    (here / "short_test.jsonl").write_text("\n".join(test) + "\n", encoding="utf-8")
    print("examples.jsonl: %d generated + %d hand-labeled short rows; short_test.jsonl: %d rows"
          % (len(kept), len(added), len(test)))


if __name__ == "__main__":
    main()
