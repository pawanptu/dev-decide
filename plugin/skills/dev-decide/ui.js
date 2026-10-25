// The Dev Decide web console (ui.html), as Alpine.js components. It runs on Alpine's CSP build (vendor/), so the
// page needs no eval and no inline script: every value a template shows is computed here, and templates only read
// properties and call methods.
(function () {
  "use strict";

  var LOCAL = { rule: 1, learned: 1, memory: 1, episodic: 1, cache: 1 };
  var DIFF_TYPES = { pr: 1, deploy: 1, risk: 1 };
  var EXAMPLES = {
    triage: { label: "Bug report", state: "Login returns 500 for users with a plus sign in their email address. Started after yesterday's deploy. About 3% of sign-ins fail." },
    pr: { label: "Pull request", state: "Extend session lifetime to 30 days",
          diff: "diff --git a/src/auth/session.py b/src/auth/session.py\n--- a/src/auth/session.py\n+++ b/src/auth/session.py\n@@ -10,7 +10,7 @@ def make_session(user):\n-    ttl = 3600\n+    ttl = 86400 * 30\n     token = secrets.token_urlsafe(32)\n     return Session(user=user, token=token, ttl=ttl)\n" },
    incident: { label: "Error log", state: "Traceback (most recent call last):\n  File \"worker.py\", line 88, in run\n    batch = model(inputs.cuda())\ntorch.cuda.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB" },
    plan: { label: "Task", state: "Add rate limiting to the public login endpoint: 5 attempts per minute per IP, with a clear error message." },
    route: { label: "Request", state: "Rename the user_email column to email in Postgres without downtime." },
    deploy: { label: "Deploy", state: "Drop the legacy orders_v1 table",
              diff: "diff --git a/migrations/0042_drop_orders_v1.sql b/migrations/0042_drop_orders_v1.sql\nnew file mode 100644\n--- /dev/null\n+++ b/migrations/0042_drop_orders_v1.sql\n@@ -0,0 +1,2 @@\n+DROP TABLE orders_v1;\n+ALTER TABLE orders ALTER COLUMN total TYPE numeric(12,2);\n" },
    risk: { label: "Risky diff", state: "Disable TLS verification for the payments client",
            diff: "diff --git a/src/payments/client.py b/src/payments/client.py\n--- a/src/payments/client.py\n+++ b/src/payments/client.py\n@@ -21,4 +21,4 @@ def charge(amount, card):\n-    resp = requests.post(URL, json=body, timeout=10)\n+    resp = requests.post(URL, json=body, timeout=10, verify=False)\n     return resp.json()\n" }
  };
  var QUESTION_NAMES = { triage: "Triage", pr: "Pull request", incident: "Incident", plan: "Plan", route: "Route", deploy: "Deploy", risk: "Risk" };
  var PROVIDER_NAMES = { anthropic: "Anthropic", openai: "OpenAI-compatible", gemini: "Gemini" };
  var KEY_HEADERS = { anthropic: "X-Anthropic-Api-Key", openai: "X-OpenAI-Api-Key", gemini: "X-Gemini-Api-Key" };
  var KEY_PLACEHOLDERS = {
    anthropic: "sk-ant-… lets Claude answer what stays unsure",
    openai: "Your key for this server's OpenAI-compatible model",
    gemini: "Your Gemini API key"
  };
  var MODE_HINT = {
    local: "Instant and free. Questions without a trusted local answer show as “not decided”.",
    hybrid: "Local first; Claude answers only what stays unsure.",
    llm: "Every question goes to Claude. Slower, and it uses a model call."
  };
  var VIA = {
    rule: "rule", learned: "learned model", memory: "memory", episodic: "memory", cache: "cache",
    llm: "Claude", "llm-strong": "stronger Claude", hint: "rough guess", none: "not decided"
  };
  var HERO = [
    { cmd: "/dev-decide triage :: Login returns 500 for users with a plus sign…", tier: 1,
      rows: [{ k: "issue_type", v: "bug", c: "89%", src: "learned model · 1 ms", tone: "ok" },
             { k: "severity", v: "sev3_minor", c: "40%", src: "unsure → asks you", tone: "warn" }] },
    { cmd: "/dev-decide pr :: DIFF", tier: 0, verdict: "review", why: "needs_tests is likely, so a person takes a look",
      rows: [{ k: "secret_leak", v: "no", c: "96%", src: "rule · 3 ms", tone: "ok" },
             { k: "breaking_change", v: "no", c: "84%", src: "learned model · 1 ms", tone: "ok" },
             { k: "needs_tests", v: "yes", c: "74%", src: "Claude · 2.1 s", tone: "ok" }] },
    { cmd: "/dev-decide route :: rename the user_email column in Postgres", tier: 1,
      rows: [{ k: "route", v: "db-migrate", c: "93%", src: "learned model · 2 ms", tone: "ok" }] }
  ];
  var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  var store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }
  };
  var session = {
    get: function (k) { try { return sessionStorage.getItem(k) || ""; } catch (e) { return ""; } },
    set: function (k, v) { try { sessionStorage.setItem(k, v); } catch (e) {} }
  };
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  applyTheme(store.get("dd_theme", "system"));

  function answerValue(a) {
    if (a.type === "choose") return String(a.choice);
    if (a.type === "score") return a.score + " / 5";
    return (a.verdict ? "yes" : "no") + "  ·  " + Math.round(100 * a.p_true) + "% likely";
  }
  function human(key) { return String(key).replace(/_/g, " "); }
  function ago(t) {
    var d = (Date.now() - t) / 1000;
    if (d < 45) return "just now";
    if (d < 3600) return Math.round(d / 60) + " min ago";
    if (d < 86400) return Math.round(d / 3600) + " h ago";
    return new Date(t).toLocaleDateString([], { month: "short", day: "numeric" });
  }

  document.addEventListener("alpine:init", function () {
    var Alpine = window.Alpine;

    // ---------- the whole page: theme, toast, copy, server status
    Alpine.data("page", function () {
      return {
        theme: store.get("dd_theme", "system"),
        toastText: "", toastOn: false, scrolled: false,
        health: null, healthText: "checking the server…", healthState: "wait",
        init: function () {
          var self = this;
          window.addEventListener("scroll", function () { self.scrolled = window.scrollY > 8; }, { passive: true });
          fetch("/health").then(function (r) { return r.json(); }).then(function (d) {
            self.health = d; self.healthState = "ok";
            var claude = d.local_only ? "local answers (your own key for Claude)" : (d.model === "none" ? "local answers only" : "Claude available");
            self.healthText = "Online · v" + d.version + " · " + claude;
            window.dispatchEvent(new CustomEvent("dd-health", { detail: d }));
          }).catch(function () {
            self.healthState = "bad"; self.healthText = "Unreachable (it may be waking up)";
            window.dispatchEvent(new CustomEvent("dd-health", { detail: null }));
          });
        },
        themeLabel: function () { return { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" }[this.theme]; },
        cycleTheme: function () {
          var order = ["system", "light", "dark"];
          this.theme = order[(order.indexOf(this.theme) + 1) % 3];
          store.set("dd_theme", this.theme); applyTheme(this.theme); this.toast(this.themeLabel());
        },
        toast: function (msg) {
          var self = this; this.toastText = msg; this.toastOn = true;
          clearTimeout(this._t); this._t = setTimeout(function () { self.toastOn = false; }, 1900);
        },
        copy: function (text) {
          var self = this;
          (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(
            function () { self.toast("Copied"); }, function () { self.toast("Select the text and copy it"); });
        },
        go: function (id) { var n = document.getElementById(id); if (n) n.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "start" }); }
      };
    });

    // ---------- the hero: an example decision, typed out and answered
    Alpine.data("hero", function () {
      return {
        i: 0, typed: "", shown: 0, tierAt: -1, verdictOn: false, scen: HERO[0], paused: false,
        init: function () { if (REDUCED) { this.finish(); return; } this.play(); },
        finish: function () { this.typed = this.scen.cmd; this.tierAt = this.scen.tier; this.shown = this.scen.rows.length; this.verdictOn = !!this.scen.verdict; },
        play: function () {
          var self = this, s = this.scen, n = 0;
          this.typed = ""; this.shown = 0; this.tierAt = -1; this.verdictOn = false;
          var type = setInterval(function () {
            n += 2; self.typed = s.cmd.slice(0, n);
            if (n >= s.cmd.length) { clearInterval(type); step(0); }
          }, 28);
          function step(t) {
            self.tierAt = t;
            if (t < s.tier) { setTimeout(function () { step(t + 1); }, 320); return; }
            setTimeout(rows, 380);
          }
          function rows() {
            if (self.shown < s.rows.length) { self.shown += 1; setTimeout(rows, 360); return; }
            if (s.verdict) self.verdictOn = true;
            setTimeout(function () { self.next(); }, 2800);
          }
        },
        next: function () { this.i = (this.i + 1) % HERO.length; this.scen = HERO[this.i]; this.play(); },
        tierClass: function (t) { return (this.tierAt >= t ? "on" : "") + (this.tierAt === t && this.shown > 0 ? " stop" : ""); },
        rowOn: function (i) { return i < this.shown; },
        cursorOn: function () { return this.typed.length < this.scen.cmd.length; }
      };
    });

    // ---------- numbers that count up when they come into view
    Alpine.data("countup", function (to, decimals, prefix, suffix) {
      return {
        v: REDUCED ? to : 0,
        init: function () {
          if (REDUCED) return;
          var self = this, done = false;
          var io = new IntersectionObserver(function (entries) {
            if (done || !entries[0].isIntersecting) return;
            done = true; io.disconnect();
            var t0 = performance.now();
            (function frame(t) {
              var p = Math.min(1, (t - t0) / 1100), e = 1 - Math.pow(1 - p, 3);
              self.v = to * e;
              if (p < 1) requestAnimationFrame(frame);
            })(t0);
          }, { threshold: 0.4 });
          io.observe(this.$el);
        },
        text: function () { return prefix + this.v.toFixed(decimals) + suffix; }
      };
    });

    // ---------- the live console
    Alpine.data("console", function () {
      return {
        health: null,
        token: "", ownToken: session.get("dd_token"), apiKey: "", keyProvider: "anthropic", accessOpen: false,
        shape: "single", q: "triage", mode: "local",
        stateText: "", diffText: "", batchText: "",
        loading: false, elapsed: 0, loadStep: 0,
        result: null, history: store.get("dd_history_v1", []), presets: null, lastBatch: [],
        examples: Object.keys(EXAMPLES).map(function (k) { return { key: k, label: EXAMPLES[k].label }; }),

        init: function () {
          var self = this;
          window.addEventListener("dd-health", function (e) {
            self.health = e.detail;
            if (self.health && self.health.demo_token && !self.ownToken) self.token = self.health.demo_token;
            else self.token = self.ownToken;
            if (!self.token) self.accessOpen = true;
            self.fixMode();
          });
          setInterval(function () { self.history = self.history.slice(); }, 60000);  // refresh "x min ago"
          window.addEventListener("dd-try", function (e) { self.tryBundle(e.detail); });
        },

        // access
        usingDemo: function () { return !!(this.health && this.health.demo_token && this.token === this.health.demo_token); },
        hasDemo: function () { return !!(this.health && this.health.demo_token); },
        providers: function () { return (this.health && this.health.own_keys) || ["anthropic"]; },
        providerName: function (p) { return PROVIDER_NAMES[p] || p; },
        keyPlaceholder: function () { return KEY_PLACEHOLDERS[this.keyProvider] || ""; },
        accessSummary: function () {
          var key = "your " + this.providerName(this.keyProvider) + " key";
          if (this.usingDemo()) return this.apiKey ? "Demo token + " + key : "Public demo token · local answers, free";
          if (this.token) return this.apiKey ? "Your token + " + key : "Your own token";
          return "Add a token to start";
        },
        accessNote: function () {
          return this.usingDemo() ? "Using the public demo token: local answers, free. Add your own API key below to let a model answer too."
                                  : "Using your own token.";
        },
        toggleToken: function () {
          if (this.usingDemo()) { this.token = this.ownToken; this.accessOpen = true; var self = this; this.$nextTick(function () { if (self.$refs.token) self.$refs.token.focus(); }); }
          else if (this.health) { this.token = this.health.demo_token; }
          this.fixMode();
        },
        tokenInput: function () { if (!this.usingDemo()) { this.ownToken = this.token; session.set("dd_token", this.token); } this.fixMode(); },
        canClaude: function () {
          var h = this.health;
          var server = !!h && h.model !== "none" && !h.local_only && h.model_calls_left !== false && !this.usingDemo();
          return !!this.apiKey.trim() || server;
        },
        modeLocked: function (m) { return m !== "local" && !this.canClaude(); },
        modeTitle: function (m) {
          if (!this.modeLocked(m)) return "";
          return this.usingDemo() ? "Add your own API key (Access) to ask a model with the demo token." : "This server cannot ask a model right now; add your own API key.";
        },
        toggleAccess: function () { this.accessOpen = !this.accessOpen; },
        setShape: function (s) { this.shape = s; },
        fixMode: function () { if (this.modeLocked(this.mode)) this.mode = "local"; },
        setMode: function (m) { if (!this.modeLocked(m)) this.mode = m; else { this.accessOpen = true; } },
        modeHint: function () { return MODE_HINT[this.mode] + (this.modeLocked("hybrid") && this.usingDemo() ? " Add your own key to ask Claude." : ""); },
        capWarning: function () {
          var mc = this.health && this.health.model_calls;
          if (!mc || !mc.warning) return "";
          return "This server has used " + mc.used + " of " + mc.cap + " Claude calls today (" + Math.round(100 * mc.share) + "%). " +
                 (mc.used >= mc.cap ? "New requests are answered locally until tomorrow (UTC), unless you add your own key." : "Answers switch to local when the cap is reached.");
        },

        // input
        setQ: function (k) { this.q = k; },
        qName: function (k) { return QUESTION_NAMES[k] || k; },
        needsDiff: function () { return this.shape === "single" && !!DIFF_TYPES[this.q]; },
        useExample: function (k) {
          this.shape = "single"; this.q = k; this.stateText = EXAMPLES[k].state; this.diffText = EXAMPLES[k].diff || "";
        },
        tryBundle: function (k) { this.useExample(k); var n = document.getElementById("try"); if (n) n.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth" }); },
        batchCount: function () { return this.batchText.split("\n").filter(function (x) { return x.trim(); }).length; },
        inputCount: function () { return this.stateText.length ? this.stateText.length + " characters" : ""; },

        // requests
        api: function (path, body) {
          var headers = { "Authorization": "Bearer " + this.token.trim(), "Content-Type": "application/json" };
          if (this.apiKey.trim()) headers[KEY_HEADERS[this.keyProvider] || KEY_HEADERS.anthropic] = this.apiKey.trim();
          return fetch(path, { method: body ? "POST" : "GET", headers: headers, body: body ? JSON.stringify(body) : undefined })
            .then(function (r) { return r.json().then(function (data) { return { ok: r.ok, status: r.status, data: data }; }); });
        },
        fail: function (msg) { this.result = { kind: "error", message: msg }; },
        decide: function () {
          if (this.loading) return;
          var self = this, body = { questions: [this.q], mode: this.mode, why: false };
          if (!this.token.trim()) { this.fail("Enter your access token first (Access, above)."); this.accessOpen = true; return; }
          if (this.shape === "batch") {
            this.lastBatch = this.batchText.split("\n").map(function (x) { return x.trim(); }).filter(Boolean).slice(0, 50);
            if (!this.lastBatch.length) { this.fail("Add some inputs, one per line."); return; }
            body.items = this.lastBatch.map(function (x, i) { return { id: String(i + 1), state: x }; });
          } else {
            var state = this.stateText.trim(), diff = this.needsDiff() ? this.diffText.trim() : "";
            if (!state && !diff) { this.fail("Add some text or a diff to decide about."); return; }
            if (state) body.state = state;
            if (diff) body.diff = diff;
          }
          this.loading = true; this.result = null; this.elapsed = 0; this.loadStep = 0;
          var t0 = performance.now();
          var tick = setInterval(function () {
            self.elapsed = Math.round(performance.now() - t0);
            self.loadStep = Math.min(3, Math.floor(self.elapsed / 450));
          }, 60);
          this.api("/v1/decide", body).then(function (r) {
            self.show(r.data, r.status);
            if (r.ok) self.remember(body, r.data);
          }).catch(function () {
            self.fail("Could not reach the server. If it was asleep, wait a minute and try again.");
          }).then(function () { clearInterval(tick); self.loading = false; });
        },
        loadLabel: function () { return ["Checking rules", "Asking the learned model", "Recalling similar inputs", "Asking Claude about what stays unsure"][this.loadStep]; },
        loadOn: function (i) { return this.loadStep >= i; },

        // results
        show: function (data, status) {
          var self = this;
          if (data.error && !data.answers && !data.items) {
            this.fail((status === 401 ? "Wrong or missing token. " : "") + data.error);
            return;
          }
          var meta = data.meta || {}, via = meta.via || {}, local = 0, claude = 0, guess = 0, open = 0;
          Object.keys(via).forEach(function (k) {
            if (LOCAL[k]) local += via[k]; else if (k.indexOf("llm") === 0) claude += via[k]; else if (k === "hint") guess += via[k]; else open += via[k];
          });
          var total = local + claude + guess + open || 1;
          var res = {
            kind: data.items ? "batch" : "single", id: data.id, raw: JSON.stringify(data, null, 2), rawOpen: false,
            warnings: (meta.warnings || []).slice(0, 4),
            ms: meta.ms ? meta.ms.total : null, cost: meta.cost_usd || null, ownKey: !!meta.own_key,
            sources: [
              { key: "local", label: "trusted local", n: local, pct: 100 * local / total },
              { key: "claude", label: "by Claude", n: claude, pct: 100 * claude / total },
              { key: "guess", label: "rough guess", n: guess, pct: 100 * guess / total },
              { key: "open", label: "not decided", n: open, pct: 100 * open / total }
            ].filter(function (s) { return s.n > 0; }),
            gates: [], answers: [], rows: [], verdicts: []
          };
          if (data.items) {
            var counts = {};
            data.items.forEach(function (it, i) {
              var ans = it.answers || {}, parts = [];
              Object.keys(ans).slice(0, 3).forEach(function (k) {
                var a = ans[k]; if (!a.error) parts.push(human(k) + ": " + answerValue(a) + (a.via === "hint" ? " (guess)" : a.abstain ? " ?" : ""));
              });
              var g = it.gates || {}, gk = Object.keys(g)[0], v = gk ? g[gk].verdict : "";
              if (v) counts[v] = (counts[v] || 0) + 1;
              res.rows.push({ id: it.id || String(i + 1), input: (self.lastBatch[i] || "").slice(0, 140),
                              answer: it.error ? it.error : (parts.join(" · ") || "not decided"), verdict: v });
            });
            res.verdicts = ["pass", "review", "block"].filter(function (v) { return counts[v]; }).map(function (v) { return { v: v, n: counts[v] }; });
          } else {
            var gates = data.gates || {};
            res.gates = Object.keys(gates).map(function (g) {
              var v = gates[g], why = (v.fired || []).concat((v.uncertain || []).map(function (u) { return human(u) + " is unsure"; }));
              return { name: g, verdict: v.verdict, why: why.slice(0, 3).join("  ·  "),
                       headline: { pass: "Good to go", review: "A person should take a look", block: "Stop: fix this first" }[v.verdict] || v.verdict };
            });
            var canFb = !!(this.health && this.health.learning && !this.usingDemo() && data.id);
            var answers = data.answers || {};
            res.answers = Object.keys(answers).map(function (k, i) {
              var a = answers[k], v = a.via || "", item = { key: k, label: human(k), delay: "transition-delay:" + (i * 70) + "ms", w: 0, fbOn: canFb,
                                                            fb: { state: "idle", options: [], choice: "", msg: "" } };
              if (a.error) {
                item.state = "none"; item.value = self.mode === "local" ? "not decided locally" : a.error; item.viaLabel = "not decided"; item.viaKey = "open";
                return item;
              }
              item.value = answerValue(a);
              item.state = v === "hint" ? "guess" : a.abstain ? "unsure" : "sure";
              item.viaKey = LOCAL[v] ? "local" : v.indexOf("llm") === 0 ? "claude" : v === "hint" ? "guess" : "open";
              item.viaLabel = VIA[v] || v;
              item.pct = Math.round(100 * (a.confidence || 0));
              item.conf = item.state === "guess" ? "Local heuristic only, not a trusted answer" :
                          item.pct + "% sure" + (a.abstain ? " · below the bar, please check it" : "");
              return item;
            });
          }
          var rank = { sure: 0, unsure: 1, guess: 2, none: 3 };
          res.answers.sort(function (a, b) { return rank[a.state] - rank[b.state]; });  // trusted answers first
          res.answers.forEach(function (a, i) { a.delay = "transition-delay:" + (i * 70) + "ms"; });
          this.result = res;
          this.$nextTick(function () {
            requestAnimationFrame(function () {  // through the reactive copy, so the bars grow on screen
              var r = self.result;
              if (!r || !r.answers) return;
              r.answers.forEach(function (a) { a.w = a.pct || 0; a.shown = true; });
              r.sourcesOn = true;
            });
            var out = document.getElementById("result");
            if (out && window.innerWidth < 900) out.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth", block: "start" });
          });
        },
        toggleRaw: function () { this.result.rawOpen = !this.result.rawOpen; },
        ansClass: function (a) { return a.state + (a.shown ? " in" : ""); },
        barStyle: function (a) { return "width:" + a.w + "%"; },
        srcStyle: function (s) { return "width:" + (this.result && this.result.sourcesOn ? s.pct : 0) + "%"; },
        hasGates: function () { return !!(this.result && this.result.gates && this.result.gates.length); },

        // feedback: right, or the correct answer
        loadPresets: function () {
          var self = this;
          if (this.presets) return Promise.resolve(this.presets);
          return this.api("/v1/presets").then(function (r) { self.presets = (r.data && r.data.presets) || {}; return self.presets; });
        },
        sendFeedback: function (a, value, label) {
          var self = this, body = { id: this.result.id, answers: {} };
          body.answers[a.key] = value;
          this.api("/v1/feedback", body).then(function (r) {
            a.fb.state = r.ok ? "done" : "err"; a.fb.msg = r.ok ? label : (r.data.error || "could not save");
            if (r.ok) self.$dispatch("dd-toast", "Thanks, it learns from this");
          }).catch(function () { a.fb.state = "err"; a.fb.msg = "could not reach the server"; });
        },
        fbRight: function (a) { this.sendFeedback(a, "ok", "Confirmed. It learns from this."); },
        fbWrong: function (a) {
          var self = this;
          this.loadPresets().then(function () {
            var p = self.presets && self.presets[a.key], opts = [];
            if (p && p.type === "choose") opts = p.options;
            else if (p && p.type === "score") { for (var i = p.min; i <= p.max; i++) opts.push(String(i)); }
            else opts = ["true", "false"];
            a.fb.options = opts; a.fb.choice = opts[0]; a.fb.state = "choose";
          });
        },
        fbSave: function (a) { this.sendFeedback(a, a.fb.choice, "Saved: " + a.label + " is " + a.fb.choice + ". It learns from this."); },
        fbCancel: function (a) { a.fb.state = "idle"; },

        // history, in this browser only
        remember: function (body, data) {
          var g = data.gates || {}, gk = Object.keys(g)[0];
          this.history.unshift({ t: Date.now(), q: this.q, mode: this.mode, shape: this.shape, state: body.state || "", diff: body.diff || "",
                                 batch: body.items ? body.items.map(function (x) { return x.state; }).join("\n") : "",
                                 verdict: gk ? g[gk].verdict : "", summary: (data.summary || "").replace(/^dev-decide \| /, "") });
          this.history = this.history.slice(0, 25);
          store.set("dd_history_v1", this.history);
        },
        restore: function (h) {
          this.shape = h.shape || "single"; this.q = h.q; this.mode = this.modeLocked(h.mode) ? "local" : h.mode;
          if (h.shape === "batch") this.batchText = h.batch; else { this.stateText = h.state; this.diffText = h.diff; }
          var n = document.getElementById("try"); if (n) n.scrollIntoView({ behavior: REDUCED ? "auto" : "smooth" });
        },
        clearHistory: function () { this.history = []; store.set("dd_history_v1", []); },
        when: function (h) { return ago(h.t); },
        histTitle: function (h) { return this.qName(h.q) + (h.shape === "batch" ? " · batch" : ""); },
        histText: function (h) { return h.summary || h.state || h.batch || ""; }
      };
    });

    // ---------- bundle cards: "Try it" fills the console with an example
    Alpine.data("bundles", function () {
      return { tryBundle: function (k) { window.dispatchEvent(new CustomEvent("dd-try", { detail: k })); } };
    });

    // ---------- install tabs
    Alpine.data("tabs", function (first) {
      return { tab: first, is: function (t) { return this.tab === t; }, set: function (t) { this.tab = t; } };
    });
  });
})();
