"""Chat-style messages, hand-labeled "none", so the route model stops sending conversation to an agent.

The generated "none" examples are all non-software tasks (cover letters, recipes), so the first route model had
never seen development-flavored chat: "where do I find this setting", "the deploy is live", "commit it but don't
push". It routed such messages to an agent at high confidence (security-auditor 0.91 for a question about a Render
setting). TRAIN rows are appended to examples.jsonl (source "hand-labeled-chat"); TEST rows, phrased differently,
are a held-out set that is never trained on (chat_test.jsonl).

  python3 dev-decide-kit/pretrained/data/route/chat_examples.py   # rewrites the hand-labeled rows
"""
import hashlib
import json
from pathlib import Path

TRAIN = [
    # status and the work so far
    "is it done yet or still running?", "what did you change in the last step?", "can you summarize what we did today",
    "ok go ahead with your recommendation", "did everything pass?", "show me the result again",
    "explain that in simpler words please", "I didn't understand the last part", "what do you recommend I choose here",
    "which option is better for me", "sounds good, continue", "stop, don't do that yet", "thanks, that worked",
    "great job, what's next?", "can you repeat the steps", "what was the accuracy before and after",
    "which models are enabled now", "list what is still pending", "anything else you need from me?",
    "is there anything I should not keep public", "what do you think about this text",
    # dashboards, settings and screens
    "where do I find this setting in the dashboard", "which button should I click now",
    "I see a popup asking to save, what should I pick", "where is the environment variables page",
    "how do I open the settings panel in VS Code", "the screen shows three options, which one",
    "I clicked it, now what", "it's live now", "done, I updated the values", "where will I get this token",
    "how do I restart it", "which field do I paste this into", "I can't find the Auto-Deploy option",
    "what should I write in the root directory field", "should I choose Docker or Python here",
    "the page is asking me to verify settings", "where i will get this setting Settings Auto-Deploy",
    "I opened the menu with Deploy latest commit and Restart service, which one",
    # notifications
    "Background command finished with exit code 0", "[SYSTEM NOTIFICATION] task completed",
    "the build finished, check it", "CI is green now", "Render says the deploy is live",
    "the training run completed", "notification: your session limit resets at 1:30am", "the server restarted",
    "Background command Retrain the models in the background completed (exit code 0)",
    # approvals, commits and pushes
    "commit it but don't push yet", "push everything now", "don't commit anything until I say",
    "yes, you can run that command", "I approve, go ahead", "try again", "go", "continue where you left off",
    "do all of them", "do option one and three",
    # the assistant and the tools
    "can you see my screen?", "how does this work with claude code", "what is a plugin in claude code",
    "how can I know if it's running or not", "are you able to access my render account", "what does auto mode mean",
    "how much did that cost", "how long will this take",
    # preferences and feedback
    "please ask me with options next time", "you are confusing me", "keep the answer short",
    "I am going to sleep, decide the rest yourself", "I'll choose from the popup", "make it simpler",
    "whatever choices I have, ask me with multiple options and I will choose from there",
    # greetings and thanks
    "good morning", "hello are you there", "thank you so much", "ok",
]

TEST = [
    "has the job finished?", "what exactly did you just do?", "give me a quick recap of everything so far",
    "fine, proceed the way you suggested", "all checks passed?", "can you show the output once more",
    "say that again more simply", "I'm lost, explain the last message", "which of these would you pick",
    "nice, what comes after this?", "where do I see the logs in the dashboard", "which option in this menu do I select",
    "there is a dropdown with save options, which one", "where is the secrets section", "ok I pressed deploy, what next",
    "the service is up", "I've added the variables", "where can I copy the key from", "how do I turn it off",
    "what goes in the build command box", "Task finished: background job exited 0", "the workflow run succeeded",
    "deploy completed successfully on render", "the background generation is done",
    "go ahead and commit but hold the push", "push it to github", "don't change anything yet", "yes run it",
    "please retry that", "keep going", "are you able to see the image I sent", "how do I know whether the hook is working",
    "what does this setting do", "what will it cost me", "how long until it's finished",
    "next time give me choices to click", "that's too complicated", "I'll be back later, carry on", "hi",
    "thanks a lot", "which items are still open", "did the numbers get better",
]

SOURCE = "hand-labeled-chat"


def row(text):
    key = hashlib.sha1(("chat|" + text).encode("utf-8")).hexdigest()[:16]
    return {"id": "route-" + key[:12], "key": key, "family": "route", "source": SOURCE,
            "input": {"kind": "text", "text": text}, "labels": {"route": "none"},
            "teacher": {"route": ["none", 0.9]}, "agree": {"route": True},
            "meta": {"labeled_by": "hand", "note": "chat and meta messages: no agent should take them"}}


def main():
    here = Path(__file__).resolve().parent
    examples = here / "examples.jsonl"
    kept = [l for l in examples.read_text(encoding="utf-8").splitlines()
            if l.strip() and json.loads(l).get("source") != SOURCE]
    added = [json.dumps(row(t), ensure_ascii=False) for t in TRAIN]
    examples.write_text("\n".join(kept + added) + "\n", encoding="utf-8", newline="\n")
    (here / "chat_test.jsonl").write_text("\n".join(json.dumps(row(t), ensure_ascii=False) for t in TEST) + "\n",
                                          encoding="utf-8", newline="\n")
    print("examples.jsonl: %d other + %d hand-labeled chat rows; chat_test.jsonl: %d rows"
          % (len(kept), len(added), len(TEST)))


if __name__ == "__main__":
    main()
