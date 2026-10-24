"""System 1: fast local analyzers.

Every answer is marked with a strength:
  exact  a verified fact (only docs changed, a live Stripe key, a CUDA OOM line).
         Used as the final answer without asking a model.
  hint   a heuristic. In the default (hybrid) mode it is handed to the model as
         evidence and the model decides; in +local mode it is used directly.
"""
import math
import re
from collections import Counter

EXACT, HINT = "exact", "hint"

class _Lazy(object):
    """A regex compiled on first use: most requests only touch a few of the patterns below."""
    __slots__ = ("pattern", "flags", "_rx")

    def __init__(self, pattern, flags=0):
        self.pattern, self.flags, self._rx = pattern, flags, None

    def _c(self):
        if self._rx is None:
            self._rx = re.compile(self.pattern, self.flags)
        return self._rx

    def search(self, *a, **k):
        return self._c().search(*a, **k)

    def match(self, *a, **k):
        return self._c().match(*a, **k)

    def findall(self, *a, **k):
        return self._c().findall(*a, **k)

    def finditer(self, *a, **k):
        return self._c().finditer(*a, **k)

    def sub(self, *a, **k):
        return self._c().sub(*a, **k)


def _lazy(pattern, flags=0):
    return _Lazy(pattern, flags)

MAX_SCAN = 400000  # characters of code scanned per item


class Answer(object):
    __slots__ = ("kind", "probs", "p", "strength", "evidence")

    def __init__(self, kind, probs=None, p=None, strength=HINT, evidence=None):
        self.kind, self.probs, self.p, self.strength = kind, probs, p, strength
        self.evidence = list(evidence or [])[:3]

    def raw(self):
        if self.kind == "verify":
            return {"type": "verify", "p_true": self.p}
        return {"type": self.kind, "probs": dict(self.probs)}


def _norm(d):
    d = {str(k): max(float(v), 0.0) for k, v in d.items()}
    s = sum(d.values()) or 1.0
    return {k: v / s for k, v in d.items() if v > 0}


def C(probs, strength=HINT, ev=None):
    return Answer("choose", probs=_norm(probs), strength=strength, evidence=ev)


def S(probs, strength=HINT, ev=None):
    return Answer("score", probs=_norm(probs), strength=strength, evidence=ev)


def V(p, strength=HINT, ev=None):
    return Answer("verify", p=min(max(float(p), 0.02), 0.98), strength=strength, evidence=ev)


def peaked(level, lo=1, hi=5, mass=0.65):
    level = min(max(int(level), lo), hi)
    d = {level: mass}
    rest = 1.0 - mass
    nbrs = [l for l in (level - 1, level + 1) if lo <= l <= hi]
    for l in nbrs:
        d[l] = rest * 0.8 / len(nbrs)
    others = [l for l in range(lo, hi + 1) if l not in d]
    for l in others:
        d[l] = rest * 0.2 / len(others)
    if not others:
        for l in nbrs:
            d[l] += rest * 0.2 / len(nbrs)
    return d


def from_scores(scores, smooth=0.04, top=6):
    """Turn keyword counts into a distribution with a little mass on the rest."""
    scores = {k: v for k, v in scores.items() if v > 0}
    if not scores:
        return None
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top]
    total = sum(v for _, v in ranked)
    d = {k: (1 - smooth) * v / total for k, v in ranked}
    d["_rest"] = smooth
    return d


# ---------------------------------------------------------------- file categories

DOC_EXT = {".md", ".mdx", ".rst", ".txt", ".adoc", ".rtf"}
STYLE_EXT = {".css", ".scss", ".sass", ".less", ".styl"}
ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".otf",
             ".mp3", ".mp4", ".wav", ".pdf", ".zip"}
DATA_EXT = {".csv", ".parquet", ".jsonl", ".tsv", ".arrow", ".feather", ".npy", ".npz", ".pkl", ".h5",
            ".pt", ".ckpt", ".safetensors", ".onnx", ".bin"}
CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".env", ".properties", ".xml"}
LOCKFILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "cargo.lock",
             "go.sum", "gemfile.lock", "composer.lock", "uv.lock", "bun.lockb", "packages.lock.json",
             "gradle.lockfile", "pubspec.lock", "podfile.lock", "mix.lock", "flake.lock"}
MANIFESTS = {"package.json", "requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg",
             "cargo.toml", "go.mod", "gemfile", "composer.json", "build.gradle", "build.gradle.kts", "pom.xml",
             "pipfile", "pubspec.yaml", "environment.yml"}
LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".vue": "javascript", ".svelte": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".swift": "swift", ".go": "go", ".rs": "rust",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".dart": "dart", ".scala": "scala", ".r": "r",
    ".jl": "julia", ".cu": "cuda", ".cuh": "cuda", ".sol": "solidity", ".sql": "sql", ".sh": "shell",
    ".bash": "shell", ".zsh": "shell", ".ps1": "shell", ".html": "html_css", ".htm": "html_css",
    ".css": "html_css", ".scss": "html_css", ".sass": "html_css", ".less": "html_css", ".ipynb": "notebook",
    ".yaml": "config_yaml_json", ".yml": "config_yaml_json", ".json": "config_yaml_json",
    ".toml": "config_yaml_json", ".ini": "config_yaml_json",
}
CODE_EXT = set(LANG_BY_EXT) - DOC_EXT - STYLE_EXT - CONFIG_EXT - {".html", ".htm"} | {
    ".lua", ".pl", ".ex", ".exs", ".erl", ".hs", ".clj", ".fs", ".vy", ".m", ".mm", ".groovy", ".tf"}
TEST_NAME = _lazy(r"(^test_.*\.py$|_test\.(py|go|rb|exs?)$|\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$|"
                       r"(Test|Tests|IT)\.(java|kt|cs)$|_spec\.rb$|^test.*\.(js|ts)$)")
ORM_MODEL = _lazy(r"(^|/)(models?\.py|models/[^/]+\.py|schema\.prisma|entities?/[^/]+\.\w+|[\w-]+\.entity\.(ts|js)|"
                       r"\w+Entity\.(java|kt)|db/schema\.rb|[\w-]+\.model\.(ts|js))$")


def _ext(name):
    return "." + name.rsplit(".", 1)[-1].lower() if "." in name.strip(".") else ""


def categorize(path):
    p = path.replace("\\", "/")
    low = p.lower()
    name = low.rsplit("/", 1)[-1]
    ext = _ext(name)
    segs = set(low.split("/")[:-1])
    if ("prompt" in name and ext not in {".py", ".ts", ".js"}) or segs & {"prompts", "prompt_templates", "prompt-templates"} \
            or ext in {".prompt", ".prompty", ".j2", ".jinja"} and ("prompt" in low or "llm" in low):
        return "prompt"
    if name in LOCKFILES:
        return "lockfile"
    if low.startswith(".github/workflows/") or low.startswith(".circleci/") or low.startswith(".buildkite/") \
            or name in {".gitlab-ci.yml", "jenkinsfile", "azure-pipelines.yml", "bitbucket-pipelines.yml", ".travis.yml"}:
        return "ci"
    if segs & {"migrations", "migration", "alembic", "migrate", "versions"} and ("migrat" in low or "alembic" in low) \
            or "prisma/migrations/" in low or "db/migrate/" in low:
        return "migration"
    if segs & {"evals", "eval", "evaluation", "evaluations"} or name.startswith("eval_") or name.startswith("evals_"):
        return "eval"
    if segs & {"test", "tests", "__tests__", "spec", "specs", "testing", "e2e", "cypress"} or TEST_NAME.search(p.rsplit("/", 1)[-1]):
        return "test"
    if ext in DOC_EXT or name in {"license", "licence", "changelog", "authors", "codeowners", "notice", "contributing"} \
            or (segs & {"docs", "doc", "documentation"} and ext not in CODE_EXT):
        return "docs"
    if ext in STYLE_EXT:
        return "style"
    if name.startswith("dockerfile") or name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"} \
            or ext in {".tf", ".tfvars", ".hcl"} or segs & {"k8s", "kubernetes", "helm", "charts", "terraform",
                                                           "infra", "deploy", "deployment", "ansible", "pulumi"}:
        return "infra"
    if name in MANIFESTS:
        return "manifest"
    if ext in ASSET_EXT:
        return "asset"
    if ext == ".ipynb":
        return "notebook"
    if ext in DATA_EXT:
        return "data"
    if ext in CONFIG_EXT or name.startswith(".env") or (name.startswith(".") and not ext):
        return "config"
    if ext == ".sql":
        return "code"
    if ext in CODE_EXT or ext in {".html", ".htm"}:
        return "code"
    return "other"


# ---------------------------------------------------------------- secrets

STRONG_SECRETS = [
    ("AWS access key", _lazy(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), 0.97),
    ("Stripe live key", _lazy(r"\b(sk|rk)_live_[0-9a-zA-Z]{16,}\b"), 0.97),
    ("Stripe test key", _lazy(r"\b(sk|rk)_test_[0-9a-zA-Z]{16,}\b"), 0.85),
    ("Anthropic API key", _lazy(r"\bsk-ant-[A-Za-z0-9_\-]{20,}"), 0.97),
    ("OpenAI API key", _lazy(r"\bsk-(?!ant-)(proj-|svcacct-)?[A-Za-z0-9_\-]{32,}\b"), 0.95),
    ("GitHub token", _lazy(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"), 0.97),
    ("Slack token", _lazy(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), 0.97),
    ("Google API key", _lazy(r"\bAIza[0-9A-Za-z_\-]{35}\b"), 0.95),
    ("Hugging Face token", _lazy(r"\bhf_[A-Za-z0-9]{30,}\b"), 0.95),
    ("SendGrid key", _lazy(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"), 0.97),
    ("private key", _lazy(r"-----BEGIN ((RSA|DSA|EC|OPENSSH|PGP|ENCRYPTED) )?PRIVATE KEY"), 0.98),
    ("JWT", _lazy(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), 0.85),
    ("password in connection string",
     _lazy(r"\b(postgres(ql)?|mysql|mariadb|mongodb(\+srv)?|redis|amqp|mssql)://[^\s:@/'\"]+:([^\s@/'\"]{4,})@"), 0.9),
]
GENERIC_SECRET = _lazy(
    r"""(?i)\b(api[_-]?key|apikey|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?token|auth[_-]?token|"""
    r"""bearer|passw(?:or)?d|pwd|private[_-]?key|credentials?)\b["']?\s*[:=]\s*["']([^"'\s]{8,})["']""")
# .env and shell style: KEY=value with no quotes (GENERIC_SECRET needs quotes, so these were missed).
ENV_SECRET = _lazy(r"(?i)^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*(?:key|secret|token|passw(?:or)?d|pwd|credentials?)"
                   r"[A-Za-z0-9_]*)\s*[:=]\s*([A-Za-z0-9_\-+/=.]{12,})\s*$")
SECRET_WORD = _lazy(r"(?i)(passw(or)?d|secret|token|api[_-]?key|apikey|private[_-]?key|credential)")
PLACEHOLDER = _lazy(r"(?i)(x{4,}|\*{3,}|\.{3}|<[^>]*>|your[_-]?|example|changeme|change_me|dummy|placeholder|"
                         r"sample|redacted|fake|\$\{|\{\{|env\(|process\.env|os\.environ|getenv|secrets\.|"
                         r"settings\.|config\.|test123|password123|^(true|false|null|none)$)")


def _entropy(s):
    if not s:
        return 0.0
    counts = Counter(s)
    n = float(len(s))
    return -sum(c / n * math.log(c / n, 2) for c in counts.values())


def _mask(s):
    s = s.strip()
    return (s[:6] + "..." if len(s) > 6 else "***") + " (%d chars)" % len(s)


# ---------------------------------------------------------------- error signatures

AI_FAILURE_SIGS = [
    ("gpu_oom", r"CUDA out of memory|OutOfMemoryError: CUDA|torch\.(cuda\.)?OutOfMemoryError|RESOURCE_EXHAUSTED.{0,60}(memory|OOM)|CUBLAS_STATUS_ALLOC_FAILED|HIP out of memory|MPS backend out of memory"),
    ("nan_or_divergence", r"(?i)loss (is|=|:)\s*nan|\bnan loss|loss.{0,20}\binf\b|detected nan|gradient overflow|nan values? (in|detected)|exploding gradient"),
    ("shape_mismatch", r"(?i)size mismatch|shapes? cannot be multiplied|mat1 and mat2 shapes|incompatible shapes|shape mismatch|expected input batch_size|dimension out of range|got input of size"),
    ("dtype_device_mismatch", r"(?i)expected all tensors to be on the same device|expected scalar type|input type \(.{0,60}\) and weight type|dtype mismatch|found at least two devices"),
    ("cuda_driver", r"(?i)CUDA driver version is insufficient|no CUDA GPUs are available|CUDA error: (?!out of memory)|NCCL error|cudaErrorNoDevice|libcudart"),
    ("api_rate_limit", r"(?i)rate[_ ]limit|\b429\b|too many requests|overloaded_error|\b529\b|RateLimitError"),
    ("api_auth_quota", r"(?i)authentication_error|invalid x-api-key|invalid api key|incorrect api key|insufficient_quota|quota exceeded|exceeded your current quota|\b401\b.{0,40}(api|key|unauthori[sz]ed)"),
    ("context_overflow", r"(?i)context length|maximum context|context_length_exceeded|prompt is too long|too many tokens|exceeds the context window|input is too long"),
    ("malformed_structured_output", r"(?i)JSONDecodeError|Expecting value: line|Unexpected token .{0,20} in JSON|OutputParserException|could not parse (the )?(llm|model) output|failed to parse .{0,40}(json|output)"),
    ("tool_call_error", r"(?i)ToolException|tool_use.{0,60}error|invalid tool|unknown tool|tool .{0,40} not found|tool_call.{0,40}(fail|error)"),
    ("agent_loop", r"(?i)max(imum)? iterations|iteration limit|GraphRecursionError|recursion limit of \d+|stopped due to (the )?(iteration|time) limit|agent (is |got )?stuck|same tool call (again|repeatedly)"),
    ("retrieval_miss", r"(?i)no relevant (documents|chunks|results|context)|retriev\w+ (returned )?(no|0|zero|empty)|empty context|similarity (score )?below"),
    ("hallucination", r"(?i)hallucinat|made[- ]up (answer|fact|citation)|fabricated|invented (a |an )?(fact|citation|api|function|url)|confidently wrong"),
    ("prompt_injection", r"(?i)prompt injection|ignore (all |the )?(previous|prior) instructions|jailbr[eo]a?k"),
    ("safety_refusal", r"(?i)(model|assistant|claude|gpt|llm) (refused|refuses|declined)|stop_reason.{0,10}refusal|content (was )?(blocked|filtered) by"),
    ("eval_regression", r"(?i)eval(uation)? (score|accuracy|pass rate) (dropped|decreased|fell|regress)|regression in (the )?(eval|accuracy)|accuracy dropped"),
    ("data_drift", r"(?i)data drift|distribution shift|feature drift|concept drift"),
    ("data_pipeline", r"(?i)DataLoader worker|collate_fn|dataset.{0,40}(not found|missing|corrupt)|tokeni[sz]\w* error|preprocess\w* (failed|error)"),
]
GENERAL_ERROR_SIGS = [
    ("null_reference", r"Cannot read propert(y|ies) of (undefined|null)|'NoneType' object (has no attribute|is not)|NullPointerException|null pointer dereference|nil pointer dereference|undefined is not an object|Object reference not set to an instance"),
    ("syntax", r"SyntaxError|IndentationError|ParseError|Unexpected token(?! .{0,20} in JSON)|unexpected EOF|expected ';'|syntax error"),
    ("type", r"TypeError|is not assignable to type|error TS2\d{3}|ClassCastException|mismatched types|cannot be cast to|InvalidCastException"),
    ("dependency", r"ModuleNotFoundError|No module named|Cannot find module|ImportError|Could not resolve dependency|No matching distribution|ERESOLVE|version conflict|unresolved import|ClassNotFoundException|cannot find package"),
    ("config_env", r"(?i)environment variable .{0,40}(not set|missing|required)|\bis not set\b|missing required (config|setting|env)|ConfigError|ImproperlyConfigured|KeyError: '[A-Z][A-Z0-9_]{3,}'"),
    ("network", r"(?i)ECONNREFUSED|ECONNRESET|ETIMEDOUT|EHOSTUNREACH|connection refused|connection reset|timed out|name or service not known|getaddrinfo|SSLError|certificate verify failed|502 bad gateway|503 service unavailable|504 gateway|socket hang up|ConnectTimeout|ReadTimeout"),
    ("auth_permission", r"(?i)401 unauthorized|403 forbidden|permission denied|EACCES|AccessDenied|not authori[sz]ed|invalid token|token expired|unauthenticated|PermissionError"),
    ("resource_limit", r"(?i)\bMemoryError|OOMKilled|JavaScript heap out of memory|no space left on device|ENOSPC|too many open files|EMFILE|exit code 137|StackOverflowError|maximum recursion depth"),
    ("concurrency", r"(?i)deadlock|race condition|ConcurrentModificationException|data race|lock wait timeout|could not serialize access"),
    ("data_db", r"(?i)IntegrityError|duplicate key value|violates (foreign key|unique|not-null|check) constraint|relation .{1,60} does not exist|no such table|no such column|OperationalError|SQLSTATE|psycopg2?\.errors|MongoServerError"),
    ("test_assertion", r"AssertionError|AssertionFailedError|Expected: .{0,80}Received:|expected .{0,60} to (equal|be|have)|FAILED tests?/|\d+ failed,|Test failed"),
    ("build_toolchain", r"(?i)make: \*\*\*|ld returned \d exit status|linker command failed|CMake Error|gradle build failed|webpack.{0,30}(error|failed)|error\[E\d{4}\]|BUILD FAILED|compilation failed|failed to compile"),
    ("logic", r"(?i)wrong (result|output|value|total)|incorrect (result|output|total|value)|off[- ]by[- ]one|returns? the wrong"),
]
AI_FAILURE_RX = [(c, _lazy(r)) for c, r in AI_FAILURE_SIGS]
GENERAL_RX = [(c, _lazy(r)) for c, r in GENERAL_ERROR_SIGS]


def _signature_matches(text, table):
    """Per line, the most specific category; returns [(line_no, category)] in text order."""
    out = []
    lines = text.splitlines()[:3000]
    caused = [i for i, l in enumerate(lines) if "Caused by" in l]
    for i, line in enumerate(lines):
        if len(line) > 2000:
            line = line[:2000]
        for cat, rx in table:
            if rx.search(line):
                out.append((i, cat))
                break
    if caused:  # Java-style chains: the root cause comes after the last "Caused by"
        last = max(caused)
        after = [(i, c) for i, c in out if i >= last]
        if after:
            out = after + [(i, c) for i, c in out if i < last]
    return out


# ---------------------------------------------------------------- AI, agent and ML patterns

AI_LIB = _lazy(r"\b(anthropic|openai|google\.generativeai|google\.genai|vertexai|langchain\w*|langgraph|llama_index|"
                    r"llamaindex|litellm|instructor|dspy|crewai|autogen|haystack|semantic_kernel|ollama|vllm|"
                    r"transformers|sentence_transformers|diffusers|peft|trl|torch|tensorflow|keras|jax|flax|sklearn|"
                    r"xgboost|lightgbm|catboost|pinecone|chromadb|weaviate|qdrant_client|faiss|pgvector|mlflow|"
                    r"wandb|onnxruntime|ultralytics|torchvision|spacy|nltk|whisper|@anthropic-ai/sdk|@ai-sdk|"
                    r"ai/rsc|@langchain|@google/generative-ai)\b")
LLM_CALL = _lazy(r"(messages\.create|chat\.completions\.create|completions\.create|responses\.create|"
                      r"generate_content|\.ainvoke\(|\.invoke\(|ChatAnthropic|ChatOpenAI|AsyncAnthropic|"
                      r"AsyncOpenAI|\bAnthropic\(|\bOpenAI\(|generateText\(|streamText\(|llm\.(predict|generate|complete)|"
                      r"client\.messages|\.chat\.send)")
ML_CODE = _lazy(r"\b(sklearn|xgboost|lightgbm|catboost|train_test_split|fit_transform|GradientBoosting\w*|"
                     r"RandomForest\w*|LogisticRegression|KMeans|torch\.nn|nn\.Module|DataLoader|keras|model\.fit)\b")
MODEL_ID = _lazy(r"""["'`]((?:claude|gpt|o[134](?:-mini)?|chatgpt|gemini|llama|meta-llama|mistral|mixtral|"""
                      r"""codestral|qwen|deepseek|command-r|grok|phi-?\d|gemma)[\w.\-:/@]*)["'`]""", re.I)
DATE_PIN = _lazy(r"(\d{8}|\d{4}-\d{2}-\d{2}|@\d{6,}|-\d{4}$|-\d{3}$|:\d{4}$|-v\d+:\d+)")
HF_LOAD = _lazy(r"from_pretrained\(\s*[\"'][\w.\-]+/[\w.\-]+[\"']([^)]*)\)")

STRING_LIT = _lazy(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'|"(?:[^"\\\n]|\\.)*"|\'(?:[^\'\\\n]|\\.)*\'|`(?:[^`\\]|\\.)*`')
TOOL_NAME = _lazy(r"""(?:["']name["']\s*:\s*["']([\w-]+)["']|@tool[^\n]*\n\s*(?:async\s+)?def\s+(\w+))""")
TOOL_NAME_CAPS = [
    ("shell", _lazy(r"(?i)(shell|bash|terminal|exec_?command|run_?command|cmd)")),
    ("code_exec", _lazy(r"(?i)(python|repl|eval|run_?code|execute_?code|interpreter)")),
    ("messaging", _lazy(r"(?i)(e?mail|sms|slack|message|notify|whatsapp|telegram)")),
    ("payments", _lazy(r"(?i)(pay|charge|refund|transfer|invoice|payout)")),
    ("deploy", _lazy(r"(?i)(deploy|kubectl|terraform|release|rollout)")),
    ("db_write", _lazy(r"(?i)(sql|insert|update_|delete_|drop|write_db|db_write)")),
    ("file_write", _lazy(r"(?i)(write_?file|save_?file|delete_?file|create_?file)")),
]


def _strip_strings(text):
    """Code with string literal contents removed, so words inside prompts are not mistaken for code."""
    return STRING_LIT.sub('""', text)


AI_CHANGE_RX = [
    ("prompt", _lazy(r"(?i)(system[_ ]?prompt|\bSYSTEM\s*=|\bsystem\s*[=:]\s*[\"'f`]|role\W+system|PromptTemplate|"
                          r"ChatPromptTemplate|\bprompt\w*\s*[=:]|instructions\s*[=:]|few[_-]?shot)")),
    ("model_params", _lazy(r"(?i)\b(temperature|top_p|top_k|max_tokens|max_output_tokens|max_new_tokens|"
                                r"stop_sequences|presence_penalty|frequency_penalty|reasoning_effort|thinking)\s*[=:]")),
    ("rag_retrieval", _lazy(r"(?i)(chunk_size|chunk_overlap|retriever|similarity_search|as_retriever|search_kwargs|"
                                 r"rerank|vectorstore|vector_store|hybrid_search|bm25)")),
    ("embeddings", _lazy(r"(?i)(embedding(s)?(_model)?\s*[=:]|Embeddings\(|text-embedding|embed_(documents|query)|"
                              r"SentenceTransformer\()")),
    ("agent_tools", _lazy(r"(?i)(\btools\s*[=:]|tool_choice|@tool\b|\bTool\(|StructuredTool|function_declarations|"
                               r"[\"']?input_schema[\"']?\s*:|bind_tools|mcp_servers)")),
    ("agent_orchestration", _lazy(r"(?i)(StateGraph|add_node|add_edge|AgentExecutor|create_react_agent|"
                                       r"max_iterations|max_turns|handoff|\bCrew\(|supervisor)")),
    ("guardrails_safety", _lazy(r"(?i)(guardrail|moderation|content_filter|pii_redact|redact|jailbreak|"
                                     r"allowlist|denylist|blocklist)")),
    ("structured_output", _lazy(r"(?i)(response_format|json_schema|output_schema|with_structured_output|"
                                     r"output_parser|PydanticOutputParser)")),
    ("training_code", _lazy(r"(?i)(learning_rate|\blr\s*=|optimizer|loss_fn|criterion|\.backward\(|"
                                 r"\bepochs?\s*=|batch_size\s*=|train_test_split|\.fit\(|scheduler|early_stopping)")),
    ("fine_tune", _lazy(r"(?i)(fine[_-]?tun|LoraConfig|\bpeft\b|SFTTrainer|DPOTrainer|lora_r|qlora)")),
    ("inference_serving", _lazy(r"(?i)(vllm|tritonserver|torchserve|bentoml|text-generation-inference|"
                                     r"inference_endpoint|quantiz|gguf)")),
]
BEHAVIOR_TYPES = {"prompt", "model_swap", "model_params", "rag_retrieval", "embeddings", "agent_tools",
                  "agent_orchestration", "guardrails_safety", "structured_output", "fine_tune", "training_data"}

TOOL_DEF = _lazy(r"(?i)(\"name\"\s*:\s*\"\w[\w-]*\"[^\n]{0,300}(\"description\"|\"input_schema\")|@tool\b|"
                      r"\bTool\(|StructuredTool|\btools\s*=\s*\[|bind_tools|function_declarations|"
                      r"[\"']input_schema[\"']\s*:)")
CAPABILITIES = [
    ("shell", _lazy(r"(?i)(subprocess\.|os\.system|os\.popen|shell\s*=\s*True|child_process|execSync|"
                         r"run_shell|execute_command|bash_tool|ShellTool|\"name\"\s*:\s*\"(run_)?(shell|bash|terminal)\")")),
    ("code_exec", _lazy(r"(\bexec\(|\beval\(|PythonREPL|python_repl|code_interpreter|run_code|vm\.runIn)")),
    ("file_write", _lazy(r"(?i)(write_file|writeFile|open\([^)\n]*[\"'][wa]b?\+?[\"']|\.unlink\(|rmtree|os\.remove|"
                              r"delete_file|fs\.rm)")),
    ("db_write", _lazy(r"(?i)(\bINSERT\s+INTO|\bUPDATE\s+\w+\s+SET|\bDELETE\s+FROM|\bDROP\s+TABLE|"
                            r"session\.commit|\.save\(\)|\.delete\(\))")),
    ("network_send", _lazy(r"(?i)(requests\.(post|put|delete|patch)|httpx\.(post|put|delete|patch)|"
                                r"axios\.(post|put|delete|patch)|method:\s*[\"'](POST|PUT|DELETE|PATCH))")),
    ("messaging", _lazy(r"(?i)(send_?e?mail|sendmail|smtplib|ses\.send|send_?sms|twilio|chat\.postMessage|"
                             r"slack_sdk|send_?message|whatsapp|telegram)")),
    ("payments", _lazy(r"(?i)(stripe\.|\.charges?\.|refund|payout|transfer_funds|razorpay|paypal|"
                            r"payment_?intent|create_payment)")),
    ("deploy", _lazy(r"(?i)(kubectl|terraform\s+apply|helm\s+(install|upgrade)|git\s+push|\bdeploy\(|"
                          r"boto3\.client\(\s*[\"'](ec2|iam|lambda))")),
]
DANGEROUS_CAPS = {"shell", "code_exec", "db_write", "network_send", "messaging", "payments", "deploy"}
APPROVAL = _lazy(r"(?i)(approv|confirm|human[_-]?in[_-]?the[_-]?loop|interrupt_before|\binterrupt\(|"
                      r"require_?approval|ask_?user|needs?_review|dry_?run)")
UNTRUSTED = [
    ("email", _lazy(r"(?i)(inbound_?e?mail|raw_?e?mail|imaplib|email\.message|parse_e?mail|mailbox|gmail)")),
    ("web content", _lazy(r"(?i)(requests\.get\(|httpx\.get\(|urlopen|BeautifulSoup|scrape|crawl|"
                               r"web_?fetch|WebBaseLoader|playwright|browse)")),
    ("user input", _lazy(r"(?i)(request\.(json|form|args|body|get_json)|req\.body|user_?message|user_?input|"
                              r"\binput\(|sys\.argv|ticket|chat_?history|customer_?message|query_?text)")),
    ("documents", _lazy(r"(?i)(PyPDFLoader|UnstructuredLoader|DocumentLoader|upload|read_pdf|load_documents)")),
    ("retrieved chunks", _lazy(r"(?i)(retriever|similarity_search|vectorstore|vector_store|search_results)")),
]
SINK_WITH_MODEL_OUTPUT = _lazy(
    r"(?i)(subprocess\.\w+\(|os\.system\(|\beval\(|\bexec\(|\.execute\(|sendmail\(|send_?e?mail\(|requests\.post\()"
    r"[^\n]*(\.input\[|tool_call|arguments|\.text\b|content\[0\]|completion|response\b|output\b|block\.input|args\[)")
VALIDATION = _lazy(r"(?i)(jsonschema|model_validate|\bvalidate\(|allowlist|ALLOWED_|whitelist|shlex\.quote|"
                        r"parameteri[sz]ed|re\.fullmatch|\bin\s+(ALLOWED|allowed|SAFE|safe)_)")
PII = _lazy(r"(?i)\b(email|phone|mobile|address|ssn|aadhaar|pan_?card|dob|date_of_birth|account_?number|"
                 r"transcript|customer|patient|medical|salary|loan|credit_?card|kyc|passport)\b")
PROMPT_LOGGING = _lazy(r"(?i)(log\w*\.(info|debug|warning)|print)\([^)\n]*(prompt|messages|transcript)")
LOOP = _lazy(r"^(\s*)(for\b|while\b|\.forEach\(|\.map\()")
QUERY_CALL = _lazy(r"(\.objects\.(get|filter)|\.query\(|session\.query|\.find(One|ById)?\(|\bSELECT\b|"
                        r"\.execute\(|\bfetch\(|requests\.get|axios\.get|\.findBy\w*\()")
SEED = _lazy(r"(random_state\s*=|\.seed\(|manual_seed|set_seed|seed_everything|np\.random\.default_rng\(\s*\d)")
RANDOMNESS = _lazy(r"(train_test_split\(|RandomForest\w*\(|GradientBoosting\w*\(|ExtraTrees\w*\(|KMeans\(|"
                        r"MLP\w+\(|SGD\w+\(|XGB\w+\(|LGBM\w+\(|np\.random\.|random\.(random|choice|shuffle|randint)|"
                        r"torch\.rand|shuffle\s*=\s*True)")
TEMP_POSITIVE = _lazy(r"(?i)\btemperature\s*[=:]\s*(0?\.\d*[1-9]|[1-9])")
LEAK_PREFIT = _lazy(r"(StandardScaler|MinMaxScaler|RobustScaler|MaxAbsScaler|Normalizer|OneHotEncoder|LabelEncoder|"
                         r"OrdinalEncoder|TargetEncoder|SimpleImputer|KNNImputer|IterativeImputer|PCA|TruncatedSVD|"
                         r"SelectKBest|SelectFromModel|VarianceThreshold|TfidfVectorizer|CountVectorizer|"
                         r"PolynomialFeatures)\([^)]*\)\s*\.(fit|fit_transform)\(|\b(scaler|encoder|imputer|vectori[sz]er|"
                         r"pca|selector|normali[sz]er)\w*\.(fit|fit_transform)\(")
SPLIT = _lazy(r"(train_test_split\(|KFold\(|StratifiedKFold\(|GroupKFold\(|TimeSeriesSplit\(|\.split\(\s*X)")
FUTURE_FEATURE = _lazy(r"\.shift\(\s*-\s*\d|\.rolling\([^)]*center\s*=\s*True|\.bfill\(|fillna\([^)]*bfill")
TIME_DATA = _lazy(r"(?i)(parse_dates|to_datetime|sort_values\(\s*[\"'](date|time|timestamp|day)|[\"'](date|timestamp)[\"'])")
TEST_TUNING = _lazy(r"(GridSearchCV|RandomizedSearchCV|\.fit)\(\s*X_test")

SENSITIVE_PATH = _lazy(r"(?i)(auth|login|session|token|password|permission|acl|rbac|security|crypto|payment|"
                            r"billing|checkout|invoice|stripe|admin|oauth|sso|jwt|secret|wallet)")
PUBLIC_API_PATH = _lazy(r"(?i)((openapi|swagger)\.(ya?ml|json)$|\.proto$|\.graphql$|(^|/)api/|(^|/)routes?/|"
                             r"(^|/)controllers?/|(^|/)public/|index\.(ts|js)$|__init__\.py$)")
SIGNATURE = _lazy(r"^\s*(?:export\s+(?:default\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^)]*)\)|"
                       r"(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(([^)]*)\)|func\s+(?:\([^)]*\)\s*)?([A-Z]\w*)\s*\(([^)]*)\)|"
                       r"public\s+(?:static\s+)?[\w<>\[\], ]+\s+(\w+)\s*\(([^)]*)\)|"
                       r"export\s+const\s+(\w+)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>)")
EXPORT_REMOVED = _lazy(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|interface|type)\s+(\w+)")
SQL_CONCAT = _lazy(r"(?i)(f[\"'][^\"'\n]*\b(select|insert|update|delete)\b[^\"'\n]*\{[^}]+\}|"
                        r"[\"'][^\"'\n]*\b(select|insert|update|delete)\b[^\"'\n]*[\"']\s*(\+|%)\s*[\w(]|"
                        r"\b(select|insert|update|delete)\b[^\"'\n]*[\"']\s*\.format\(|"
                        r"`[^`\n]*\b(select|insert|update|delete)\b[^`\n]*\$\{)")
DANGEROUS_CALL = _lazy(r"(\beval\(|\bexec\(|shell\s*=\s*True|os\.system\(|dangerouslySetInnerHTML|\.innerHTML\s*=|"
                            r"child_process\.exec\(|pickle\.loads?\(|yaml\.load\((?![^)]*Loader)|verify\s*=\s*False|"
                            r"disable_ssl|csrf_exempt|@csrf_exempt|CORS\(\s*\w+\s*,\s*origins\s*=\s*[\"']\*)")
SIDE_EFFECT = _lazy(r"(?i)(webhook|send_?e?mail|sendmail|smtplib|stripe\.|\.charges?\.|refund|publish\(|"
                         r"producer\.send|sns\.publish|sqs\.send|send_?sms|twilio)")
DESTRUCTIVE_MIGRATION = _lazy(r"(?i)(drop\s+(table|column)|rename\s+(column|to)|alter\s+column[^;\n]*type|"
                                   r"op\.drop_|RemoveField|DeleteModel|RenameField|RenameModel|AlterField|"
                                   r"remove_column|rename_column|drop_table|DROP\s+INDEX)")
BACKFILL = _lazy(r"(?i)(\bUPDATE\s+\w+\s+SET|\bDELETE\s+FROM|RunPython|op\.execute\(|bulk_update|backfill)")
ADDITIVE_MIGRATION = _lazy(r"(?i)(create\s+table|add\s+column|create\s+(unique\s+)?index|AddField|CreateModel|"
                                r"op\.add_column|op\.create_|add_column|create_table|add_index)")
DDL = _lazy(r"(?i)\b(alter\s+table|create\s+table|drop\s+table|add\s+column|drop\s+column|create\s+(unique\s+)?index|"
                 r"rename\s+column)\b")
ORM_FIELD = _lazy(r"(Column\(|models\.\w+Field\(|@Column|mapped_column\(|^\s*\w+\s+(String|Int|Boolean|DateTime|"
                       r"Float|Decimal|Json)\b|t\.(string|integer|boolean|datetime))")
VERSION_PKG_JSON = _lazy(r"\"(@?[\w./-]+)\"\s*:\s*\"[\^~>=<]*v?(\d+)\.")
VERSION_REQ = _lazy(r"^([A-Za-z0-9_.\-\[\]]+)\s*(?:==|>=|~=|===)\s*v?(\d+)")


# ---------------------------------------------------------------- text heuristics

def _rx(pattern):
    return _lazy(pattern, re.I | re.M)


TASK_WORDS = {
    "bugfix": _rx(r"\b(fix\w*|bug|crash\w*|broken|errors?|fails?|failing|regression|exception|incorrect|wrong)\b"),
    "feature": _rx(r"\b(add|implement|support|new|introduce|create|build|enable|allow)\b"),
    "refactor": _rx(r"\b(refactor\w*|clean ?up|restructure|rename|simplify|extract|reorgani[sz]e|dedupe)\b"),
    "performance": _rx(r"\b(slow|performance|latency|speed ?up|optimi[sz]e|faster|memory usage|cpu usage)\b"),
    "security": _rx(r"\b(security|vulnerab\w*|cve-\d+|xss|csrf|injection|sanitiz\w*|exploit|auth bypass)\b"),
    "test": _rx(r"\b(unit tests?|test coverage|add tests?|flaky tests?|e2e tests?)\b"),
    "docs": _rx(r"\b(docs?|documentation|readme|docstrings?)\b"),
    "build_ci": _rx(r"\b(ci|github actions|workflow|build script|makefile|pipeline config)\b"),
    "dependency": _rx(r"\b(upgrade|bump|dependenc\w+|package version|update (the )?(sdk|library|package))\b"),
    "infra": _rx(r"\b(terraform|kubernetes|k8s|helm|infrastructure|autoscal\w+|load balancer)\b"),
    "data_migration": _rx(r"\b(migrat\w+|schema change|backfill|alter table)\b"),
    "ui_ux": _rx(r"\b(ui|ux|button|layout|css|styling|responsive|dark mode|modal|navbar)\b"),
    "ml_experiment": _rx(r"\b(experiment|notebook|explore the data|hyperparameter)\b"),
    "model_training": _rx(r"\b(train(ing)? (a |the )?model|fine-?tun\w+|lora|epochs?|checkpoint)\b"),
    "prompt_engineering": _rx(r"\b(prompt|system prompt|few-?shot)\b"),
    "eval": _rx(r"\b(evals?|evaluation set|benchmark|golden set|llm-as-judge)\b"),
    "question": _rx(r"(^\s*(how|what|why|where|which|can|does|is there)\b[^\n]*\?\s*$)"),
}
COMMIT_FROM_TASK = {"feature": "feat", "bugfix": "fix", "refactor": "refactor", "performance": "perf", "test": "test",
                    "docs": "docs", "build_ci": "ci", "dependency": "build", "security": "fix", "infra": "chore",
                    "data_migration": "feat", "ui_ux": "feat", "ml_experiment": "feat", "model_training": "feat",
                    "prompt_engineering": "feat", "eval": "test", "question": "chore"}
ISSUE_WORDS = {
    "bug": _rx(r"\b(crash\w*|errors?|exception|broken|fails?|failing|500|stack ?trace|traceback|doesn'?t work|"
               r"does not work|regression|wrong|incorrect)\b"),
    "feature_request": _rx(r"\b(feature request|would be (nice|great|helpful)|please (add|support)|it would help|"
                           r"add (a|an|support)|support for|could you add|wish)\b"),
    "question": _rx(r"(\bhow (do|can|to|does)\b|\bis it possible\b|\bwhat is\b|\bwhy does\b|\?\s*$)"),
    "docs": _rx(r"\b(docs?|documentation|readme|typo in the docs)\b"),
    "support": _rx(r"\b(log ?in|account|password reset|billing|refund|can'?t access|cannot access|my order|"
                   r"subscription|invoice)\b"),
    "duplicate_or_noise": _rx(r"(^\s*(\+1|same here|bump|test|asdf)\s*$|\bduplicate of\b)"),
}
SEVERITY_WORDS = {
    "sev1_outage": _rx(r"\b(outage|site is down|is down for|all users|every(one| user)|data loss|production down|"
                       r"security breach|cannot (log ?in|check ?out|pay)[^\n]{0,30}(all|every))"),
    "sev2_major": _rx(r"\b(major|blocking|blocker|core (feature|flow)|no workaround|cannot (log ?in|pay|check ?out)|"
                      r"broken for (many|most)|many (users|customers)|500 errors?)\b"),
    "sev3_minor": _rx(r"\b(workaround|minor|some users|intermittent\w*|edge case|occasionally|only when)\b"),
    "sev4_cosmetic": _rx(r"\b(typo|cosmetic|alignment|misaligned|color|colour|spacing|tooltip|wording|padding)\b"),
    "not_a_problem": _rx(r"\b(works as (intended|expected|designed)|not a bug|expected behaviou?r)\b"),
}
FLAKY_WORDS = _rx(r"\b(intermittent\w*|sometimes|flaky|flakes?|randomly|occasionally|race|timing|only on ci|"
                  r"passes locally|non-?deterministic|1 in \d+|now and then)\b")
DETERMINISTIC_WORDS = _rx(r"\b(always|every time|consistently|reproducible|100%|deterministic)\b")
PROD_WORDS = _rx(r"\b(production|prod\b|live site|customers?|users? (are|were) affected|outage|real users|paying)\b")
NONPROD_WORDS = _rx(r"\b(locally|local machine|dev(elopment)? (env|environment|server)|staging|on ci|in ci|"
                    r"my machine|nightly job|test env|unit tests?|notebook|sandbox)\b")
CLEAR_WORDS = _rx(r"\b(should|must|expected|acceptance criteria|steps to reproduce|given .{0,40} when|so that|"
                  r"returns?|endpoint|field|column|file|function|error message)\b")
VAGUE_WORDS = _rx(r"\b(fix it|make it better|improve (it|things)|clean (it|things) up|something|stuff|etc|asap|"
                  r"somehow|whatever|optimi[sz]e everything)\b")
HAIKU_WORDS = _rx(r"\b(rename|typo|format\w*|bump|lint|one-?line|small|simple|trivial|update the readme|comment)\b")
OPUS_WORDS = _rx(r"\b(architecture|design|race condition|concurrency|deadlock|security|threat model|distributed|"
                 r"migration strategy|hard to reproduce|intermittent|across (the )?(codebase|services)|"
                 r"performance tuning|memory leak|refactor (the )?(whole|entire))\b")
EFFORT_WORDS = {
    1: _rx(r"\b(typo|rename|one-?line|bump|tweak|small fix)\b"),
    2: _rx(r"\b(add (a|an) (field|button|endpoint|column|test|flag|option)|small feature|simple)\b"),
    3: _rx(r"\b(implement|integrate|feature|new endpoint|refactor)\b"),
    4: _rx(r"\b(redesign|migrate|rewrite|overhaul|multi-?tenant|new service)\b"),
    5: _rx(r"\b(platform|architecture|from scratch|re-?architect|entire system)\b"),
}
DOMAIN_SIGS = {
    "frontend": r"\.(tsx|jsx|vue|svelte|css|scss|html)\b|\b(react|nextjs|next\.js|vue|svelte|angular|tailwind|component|frontend|browser)\b",
    "backend": r"\b(api|endpoints?|server|controllers?|routes?|fastapi|flask|django|express|nestjs|spring|rails|graphql|grpc|backend|middleware)\b",
    "mobile": r"\b(android|ios|swiftui|react-native|flutter|xcode|mobile app|app store|play store)\b|\.(swift|kt|dart)\b",
    "desktop": r"\b(electron|tauri|wpf|winforms|gtk|desktop app)\b",
    "database": r"\b(sql|postgres\w*|mysql|sqlite|mongodb|redis|schema|migrations?|indexes|orm|prisma|sqlalchemy)\b",
    "data_engineering": r"\b(airflow|dbt|spark|kafka|etl|elt|data pipeline|warehouse|snowflake|bigquery|databricks|flink)\b",
    "analytics_bi": r"\b(dashboards?|looker|metabase|tableau|power ?bi|kpis?|analytics)\b",
    "data_science": r"\b(pandas|numpy|matplotlib|seaborn|notebook|jupyter|eda|scipy|statistics)\b|\.ipynb\b",
    "ml_classic": r"\b(sklearn|scikit|xgboost|lightgbm|catboost|random ?forest|logistic ?regression|gradient ?boosting|train_test_split)\b",
    "deep_learning": r"\b(torch|pytorch|tensorflow|keras|jax|cuda|gpu|neural|backprop|lora|fine-?tun\w*)\b",
    "llm_app": r"\b(anthropic|openai|claude|gpt-?\d\w*|gemini|llm|prompts?|completions?|chat ?bot|messages\.create)\b",
    "rag": r"\b(rag|retriev\w+|vector ?(store|db)|embeddings?|pinecone|chroma\w*|weaviate|qdrant|faiss|pgvector|chunk\w*|rerank\w*)\b",
    "generative_media": r"\b(diffusers|stable diffusion|text-to-image|image generation|dall-?e|text-to-speech|video generation)\b",
    "ai_agents": r"\b(agents?|tool_use|tool calls?|langgraph|crewai|autogen|mcp|function calling|multi-agent)\b",
    "mlops": r"\b(mlflow|kubeflow|sagemaker|model registry|feature store|bentoml|triton|model serving|drift)\b",
    "computer_vision": r"\b(opencv|cv2|yolo|torchvision|object detection|segmentation|ocr)\b",
    "nlp_speech": r"\b(spacy|nltk|tokeni[sz]\w+|sentiment|whisper|speech|asr|tts)\b",
    "devops_infra": r"\b(docker\w*|kubernetes|k8s|helm|terraform|ansible|github actions|nginx|deploy\w*)\b",
    "cloud": r"\b(aws|gcp|azure|s3|lambda|ec2|cloud ?run|cloudformation|cdk|iam|serverless)\b",
    "security": r"\b(security|vulnerab\w*|xss|csrf|oauth|jwt|encrypt\w*|crypto\w*|rbac|cve)\b",
    "embedded_iot": r"\b(firmware|arduino|esp32|stm32|rtos|embedded|mqtt|iot|microcontroller|gpio|i2c|uart)\b",
    "robotics": r"\b(ros2?|rclpy|robot\w*|actuators?|lidar|slam|motion planning|servo)\b",
    "ar_vr": r"\b(arkit|arcore|openxr|webxr|visionos|oculus|unity xr|vr headset)\b",
    "game": r"\b(unity|unreal|godot|gameplay|sprites?|shaders?|game)\b",
    "blockchain_web3": r"\b(solidity|ethereum|web3|ethers|smart contracts?|blockchain|nft|defi)\b|\.sol\b",
    "hpc_scientific": r"\b(mpi|openmp|hpc|slurm|numba|fortran|simulation)\b",
    "quantum": r"\b(qiskit|cirq|quantum|qubits?|pennylane)\b",
    "testing_qa": r"\b(pytest|jest|cypress|playwright|selenium|qa|test coverage)\b",
    "networking": r"\b(tcp|udp|dns|websockets?|sockets?|proxy|vpn|bgp)\b",
    "os_systems": r"\b(kernel|syscalls?|scheduler|filesystem|device driver|allocator|posix)\b",
    "tooling_cli": r"\b(cli|argparse|click|typer|cobra|command[- ]line|subcommands?)\b",
    "docs": r"\b(docs?|documentation|readme)\b",
}
DOMAIN_RX = {d: _rx(p) for d, p in DOMAIN_SIGS.items()}


# ---------------------------------------------------------------- facts

class Facts(object):
    """Everything the analyzers observed about one item."""

    def __init__(self):
        self.facts = []          # [(strength, text)]
        self.sigs = set()        # tokens for the learned model
        self.cats = {}           # path -> category
        self.comp = set()        # composition flags: docs_only, tests_only, ...
        self.langs = Counter()
        self.secrets = []        # [(kind, masked, loc, p)]
        self.secret_words = False
        self.ai_present = False
        self.llm_present = False
        self.ml_present = False
        self.ai_change = Counter()
        self.model_ids = []      # [(id, pinned, loc)]
        self.hf_unpinned = []
        self.agent = {}
        self.leak = []           # [(p, evidence)]
        self.nondet = None
        self.cost = None
        self.pii = None
        self.migration = {}
        self.breaking = []
        self.security = []
        self.perf = []
        self.side_effects = []
        self.majors = []
        self.err_general = []
        self.err_ai = []
        self.scan_text = ""
        self.lines_changed = 0
        self.config_lines = 0
        self.scan_truncated = False  # the secret scan stopped at MAX_SCAN, so "no secret" is not a fact

    def fact(self, strength, text, sig=None):
        if (strength, text) not in self.facts:
            self.facts.append((strength, text))
        if sig:
            self.sigs.add(sig)


def _loc(f, i):
    if f.added_ln and i < len(f.added_ln):
        return "%s:%d" % (f.path, f.added_ln[i])
    return f.path


def _code_files(item):
    return [f for f in item.files if not f.binary]


def _scan_lines(item, F=None):
    """(location, line) pairs to scan: added lines of diffs, whole files, and free text."""
    out, total = [], 0
    for f in _code_files(item):
        for i, line in enumerate(f.added):
            out.append((_loc(f, i), line))
            total += len(line)
            if total > MAX_SCAN:
                if F is not None:
                    F.scan_truncated = True
                return out
    if item.text:
        for i, line in enumerate(item.text.splitlines(), 1):
            out.append(("input:%d" % i, line))
    if item.commit_message:
        for i, line in enumerate(item.commit_message.splitlines(), 1):
            out.append(("commit message:%d" % i, line))
    return out


# ---------------------------------------------------------------- analyzers

def _files(item, F):
    files = item.files
    if not files:
        return
    for f in files:
        cat = categorize(f.path)
        if cat in ("code", "config") and _ext(f.path) == ".sql" and any(DDL.search(l) for l in f.added):
            cat = "migration"
        f.category = cat
        F.cats[f.path] = cat
        F.sigs.add("cat:" + cat)
        F.sigs.add("ext:" + (_ext(f.path) or "none"))
        lang = LANG_BY_EXT.get(_ext(f.path))
        n = len(f.added) + len(f.removed) or 1
        F.lines_changed += len(f.added) + len(f.removed)
        if lang and cat not in ("docs", "lockfile", "manifest", "config", "ci", "infra"):
            F.langs[lang] += n
        elif lang == "config_yaml_json":
            F.config_lines += n
    cats = set(F.cats.values())
    nfiles = len(files)
    F.sigs.add("nfiles:%s" % ("1" if nfiles == 1 else "2-5" if nfiles <= 5 else "6-20" if nfiles <= 20 else "many"))
    F.sigs.add("lines:%s" % ("<20" if F.lines_changed < 20 else "<100" if F.lines_changed < 100 else
                             "<500" if F.lines_changed < 500 else "500+"))
    names = ", ".join(sorted(F.cats)[:4]) + (" and %d more" % (nfiles - 4) if nfiles > 4 else "")

    def only(allowed):
        return bool(cats) and cats <= allowed

    if only({"docs"}):
        F.comp.add("docs_only")
        F.fact("exact", "only documentation files changed: " + names, "docs_only")
    elif only({"test"}) or only({"test", "docs"}):
        F.comp.add("tests_only")
        F.fact("exact", "only test files changed: " + names, "tests_only")
    elif only({"lockfile", "manifest"}):
        F.comp.add("deps_only")
        F.fact("exact", "only dependency manifests or lockfiles changed: " + names, "deps_only")
    elif only({"ci"}):
        F.comp.add("ci_only")
        F.fact("exact", "only CI configuration changed: " + names, "ci_only")
    elif only({"style", "asset"}) or only({"style", "asset", "docs"}):
        F.comp.add("style_only")
        F.fact("exact", "only stylesheets or static assets changed: " + names, "style_only")
    elif only({"prompt"}) or only({"prompt", "docs"}):
        F.comp.add("prompt_only")
        F.fact("exact", "only prompt files changed: " + names, "prompt_only")
    elif only({"eval"}) or only({"eval", "docs"}):
        F.comp.add("eval_only")
        F.fact("exact", "only eval files changed: " + names, "eval_only")
    if cats <= {"docs", "style", "asset"}:
        F.comp.add("inert")
    if "test" in cats:
        F.comp.add("has_tests")
    if cats & {"code", "migration", "notebook", "prompt", "eval", "infra", "config", "manifest"}:
        F.comp.add("has_code")
    if "migration" in cats:
        F.comp.add("has_migration")
        F.fact("exact", "migration files changed: " + ", ".join(p for p, c in F.cats.items() if c == "migration")[:200],
               "migration")
    if nfiles == 1:
        F.comp.add("single_file")
    tops = set(p.split("/")[0] if "/" in p else "." for p in F.cats)
    F.comp.add("dirs:%d" % len(tops))


def _secrets(item, F):
    for loc, line in _scan_lines(item, F):
        if len(line) > 5000:
            line = line[:5000]
        if SECRET_WORD.search(line):
            F.secret_words = True
        found = False
        for kind, rx, p in STRONG_SECRETS:
            m = rx.search(line)
            if m:
                F.secrets.append((kind, _mask(m.group(0)), loc, p))
                found = True
                break
        if found:
            continue
        m = GENERIC_SECRET.search(line)
        if m:
            val = m.group(2)
            if not PLACEHOLDER.search(val) and _entropy(val) >= 3.0 and not re.fullmatch(r"[a-z_]+", val):
                F.secrets.append(("hardcoded %s" % m.group(1).lower(), _mask(val), loc, 0.75))
            continue
        m = ENV_SECRET.search(line)
        if m:
            val = m.group(2)
            # a real credential mixes letters and digits; identifiers and dotted settings paths do not
            if (not PLACEHOLDER.search(val) and _entropy(val) >= 3.0 and re.search(r"\d", val)
                    and re.search(r"[A-Za-z]", val)):
                F.secrets.append(("hardcoded %s" % m.group(1).lower(), _mask(val), loc, 0.75))
    for kind, masked, loc, p in F.secrets[:3]:
        F.fact("exact" if p >= 0.85 else "hint", "possible secret (%s) %s at %s" % (kind, masked, loc), "secret")


def _errors(item, F):
    text = "\n".join(t for t in (item.text, item.commit_message) if t)
    if not text:
        return
    F.err_ai = _signature_matches(text, AI_FAILURE_RX)
    F.err_general = _signature_matches(text, GENERAL_RX)
    for _, cat in F.err_ai[:2]:
        F.fact("hint", "error text matches AI failure signature: " + cat, "aierr:" + cat)
    for _, cat in F.err_general[:2]:
        F.fact("hint", "error text matches signature: " + cat, "err:" + cat)


def _ai(item, F):
    changed, whole = [], []
    for f in _code_files(item):
        cat = f.category or categorize(f.path)
        lines = f.added + f.removed
        changed.append((f, lines))
        if f.status == "file":
            whole.append((f, f.added))
        elif cat in ("code", "notebook", "prompt", "eval", "config") and f.status != "deleted":
            cur = item.current(f.path)
            if cur:
                whole.append((f, cur.splitlines()))
    text_lines = (item.text or "").splitlines()

    def any_rx(rx, pairs):
        return any(rx.search(l) for _, ls in pairs for l in ls[:5000])

    present_scope = changed + whole + ([(None, text_lines)] if text_lines else [])
    F.llm_present = any_rx(LLM_CALL, present_scope) or any(
        (f is not None and (f.category or categorize(f.path)) == "prompt") for f, _ in changed)
    F.ml_present = any_rx(ML_CODE, changed + whole)
    lib = any_rx(AI_LIB, present_scope)
    ids = []
    for f, ls in changed + whole:
        for i, l in enumerate(ls[:5000]):
            for m in MODEL_ID.finditer(l):
                mid = m.group(1)
                if len(mid) < 4 or not (re.search(r"\d", mid) or "latest" in mid.lower()):
                    continue
                pinned = bool(DATE_PIN.search(mid)) and "latest" not in mid.lower()
                ids.append((mid, pinned, f.path if f else "input"))
            hm = HF_LOAD.search(l)
            if hm and "revision" not in hm.group(1):
                F.hf_unpinned.append(f.path if f else "input")
    seen = set()
    F.model_ids = [x for x in ids if not (x[0] in seen or seen.add(x[0]))]
    cats = set(F.cats.values())
    F.ai_present = bool(F.llm_present or F.ml_present or lib or F.model_ids or cats & {"prompt", "eval", "notebook", "data"})
    if F.ai_present:
        F.sigs.add("ai_present")
    if F.llm_present:
        F.fact("hint", "code calls an LLM API", "llm")
    if F.ml_present:
        F.fact("hint", "machine learning code present", "ml")

    # what kind of AI change (diff lines only)
    for f, lines in changed:
        cat = f.category or categorize(f.path)
        if cat == "prompt":
            F.ai_change["prompt"] += 5
        elif cat == "eval":
            F.ai_change["eval_suite"] += 5
        elif cat == "data" and re.search(r"(?i)(train|dataset|data)/", f.path):
            F.ai_change["training_data"] += 3
        if cat not in ("code", "notebook", "config", "prompt", "eval"):
            continue
        for l in lines[:3000]:
            for kind, rx in AI_CHANGE_RX:
                if rx.search(l):
                    F.ai_change[kind] += 1
    if F.model_ids and item.kind in ("diff", "mixed"):
        removed_ids = set()
        added_ids = set()
        for f in _code_files(item):
            for l in f.removed:
                removed_ids.update(m.group(1) for m in MODEL_ID.finditer(l))
            for l in f.added:
                added_ids.update(m.group(1) for m in MODEL_ID.finditer(l))
        if removed_ids and added_ids and removed_ids != added_ids:
            F.ai_change["model_swap"] += 4
            F.fact("exact", "model changed from %s to %s" % (", ".join(sorted(removed_ids))[:60],
                                                          ", ".join(sorted(added_ids))[:60]), "model_swap")
    if not F.llm_present and not F.ml_present:
        for k in ("training_code", "model_params", "structured_output"):
            F.ai_change.pop(k, None)
    for kind, n in F.ai_change.most_common(3):
        F.sigs.add("aichange:" + kind)

    # agents, tools and model output handling (whole files)
    scope = whole if whole else changed
    alltext = "\n".join("\n".join(ls[:5000]) for _, ls in scope)[:MAX_SCAN]
    if F.llm_present and alltext:
        code = _strip_strings(alltext)
        has_tools = bool(TOOL_DEF.search(alltext))
        caps = [name for name, rx in CAPABILITIES if rx.search(code)]
        for m in TOOL_NAME.finditer(alltext):
            tname = m.group(1) or m.group(2) or ""
            for cap, rx in TOOL_NAME_CAPS:
                if rx.search(tname) and cap not in caps:
                    caps.append(cap)
                    break
        if not has_tools and not SINK_WITH_MODEL_OUTPUT.search(code):
            # No tools and no model output flowing into actions: other code in the file is not the model's capability.
            caps = [c for c in caps if c not in DANGEROUS_CAPS]
        approval = bool(APPROVAL.search(code))
        untrusted = [name for name, rx in UNTRUSTED if rx.search(code)]
        sink = bool(SINK_WITH_MODEL_OUTPUT.search(code))
        validated = bool(VALIDATION.search(code))
        F.agent = {"tools": has_tools, "caps": caps, "approval": approval, "untrusted": untrusted,
                   "sink": sink, "validated": validated}
        if has_tools:
            F.fact("hint", "the model is given tools" + (" with these capabilities: " + ", ".join(caps) if caps else ""),
                   "agent_tools")
        dangerous = [c for c in caps if c in DANGEROUS_CAPS]
        if dangerous and not approval:
            F.fact("hint", "consequential actions (%s) with no human approval step found" % ", ".join(dangerous),
                   "no_approval")
        if untrusted:
            F.fact("hint", "untrusted content reaches the model: " + ", ".join(untrusted), "untrusted_input")
        if sink and not validated:
            F.fact("hint", "model output flows into a command, query or message without validation", "unvalidated_sink")
        pii_hit = PII.search(alltext)
        logs_prompts = bool(PROMPT_LOGGING.search(alltext))
        F.pii = (bool(pii_hit), logs_prompts)
        if pii_hit:
            F.fact("hint", "personal or sensitive fields appear near model calls (%s)" % pii_hit.group(1), "pii")
        lines = alltext.splitlines()
        loop_call, unbounded = False, False
        for i, l in enumerate(lines):
            m = LOOP.match(l)
            if not m:
                continue
            indent = len(m.group(1))
            for nxt in lines[i + 1:i + 15]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                if LLM_CALL.search(nxt):
                    loop_call = True
                    if re.match(r"\s*while\s+(True|1)\b", l) and not re.search(r"(?i)max_(iter|turns|steps)|range\(", alltext):
                        unbounded = True
                    break
        removed_limit = any(re.search(r"max_tokens|max_output_tokens", l) for f in _code_files(item) for l in f.removed) and \
            not any(re.search(r"max_tokens|max_output_tokens", l) for f in _code_files(item) for l in f.added)
        F.cost = {"loop": loop_call, "unbounded": unbounded, "removed_limit": removed_limit}
        if loop_call:
            F.fact("hint", "model calls happen inside a loop" + (" with no iteration cap" if unbounded else ""),
                   "llm_in_loop")
        F.perf.append("model call inside a loop") if loop_call else None

    # machine learning hygiene
    if F.ml_present:
        for f, ls in (whole or changed):
            text = "\n".join(ls[:5000])
            lines = ls[:5000]
            split_idx = next((i for i, l in enumerate(lines) if SPLIT.search(l)), None)
            if split_idx is not None:
                for i, l in enumerate(lines[:split_idx]):
                    if LEAK_PREFIT.search(l):
                        F.leak.append((0.85, "%s:%d preprocessing fitted before the train/test split" % (f.path, i + 1)))
                        break
                if TIME_DATA.search(text) and re.search(r"train_test_split\(", text) and \
                        not re.search(r"shuffle\s*=\s*False|TimeSeriesSplit", text):
                    F.leak.append((0.6, "%s random split on time-ordered data" % f.path))
            for i, l in enumerate(lines):
                if FUTURE_FEATURE.search(l):
                    F.leak.append((0.8, "%s:%d feature uses future rows (%s)" % (f.path, i + 1, l.strip()[:50])))
                    break
            for i, l in enumerate(lines):
                if TEST_TUNING.search(l):
                    F.leak.append((0.7, "%s:%d model tuned or fitted on test data" % (f.path, i + 1)))
                    break
            rand = RANDOMNESS.search(text)
            if rand:
                F.nondet = (0.82 if not SEED.search(text) else 0.25, "%s %s" % (
                    f.path, "uses randomness without a fixed seed" if not SEED.search(text) else "sets random seeds"))
        for p, ev in F.leak[:3]:
            F.fact("hint", "possible train/test leakage: " + ev, "leakage")
    if F.llm_present and F.nondet is None:
        temp_pos = any(TEMP_POSITIVE.search(l) for _, ls in scope for l in ls[:5000])
        if temp_pos:
            F.nondet = (0.8, "sampling temperature above 0")
    if F.model_ids:
        unp = [m for m, pinned, _ in F.model_ids if not pinned]
        if unp:
            F.fact("exact" if any("latest" in m.lower() for m in unp) else "hint",
                   "model id without a pinned snapshot: " + ", ".join(unp[:3]), "model_unpinned")
        else:
            F.fact("hint", "model ids are pinned: " + ", ".join(m for m, _, _ in F.model_ids[:3]), "model_pinned")


def _code(item, F):
    if not item.files:
        return
    for f in _code_files(item):
        cat = f.category or categorize(f.path)
        added = f.added[:5000]
        removed = f.removed[:5000]
        if SENSITIVE_PATH.search(f.path) and cat in ("code", "migration", "config", "infra"):
            F.security.append((0.7, "sensitive path changed: " + f.path))
        for i, l in enumerate(added):
            if SQL_CONCAT.search(l):
                F.security.append((0.9, "%s SQL built by string formatting" % _loc(f, i)))
                break
        for i, l in enumerate(added):
            m = DANGEROUS_CALL.search(l)
            if m:
                F.security.append((0.82, "%s risky call %s" % (_loc(f, i), m.group(0)[:30])))
                break
        if cat == "migration":
            body = "\n".join(added)
            if DESTRUCTIVE_MIGRATION.search(body):
                F.migration["destructive"] = f.path
            if BACKFILL.search(body):
                F.migration["backfill"] = f.path
            if ADDITIVE_MIGRATION.search(body):
                F.migration["additive"] = f.path
        if ORM_MODEL.search(f.path) and any(ORM_FIELD.search(l) for l in added + removed):
            F.migration.setdefault("orm_models", f.path)
        if cat in ("code",) and f.status not in ("added", "file"):
            first = len(F.breaking)
            old = {}
            for l in removed:
                m = SIGNATURE.match(l)
                if m:
                    groups = [g for g in m.groups()]
                    for j in range(0, len(groups), 2):
                        if groups[j]:
                            old[groups[j]] = (groups[j + 1] or "").replace(" ", "")
            new = {}
            for l in added:
                m = SIGNATURE.match(l)
                if m:
                    groups = [g for g in m.groups()]
                    for j in range(0, len(groups), 2):
                        if groups[j]:
                            new[groups[j]] = (groups[j + 1] or "").replace(" ", "")
            for name, params in old.items():
                if name.startswith("_"):
                    continue
                if name not in new:
                    F.breaking.append((0.7, "%s: %s removed or renamed" % (f.path, name)))
                elif new[name] != params:
                    added_required = len([p for p in new[name].split(",") if p and "=" not in p and "?" not in p]) > \
                        len([p for p in params.split(",") if p and "=" not in p and "?" not in p])
                    F.breaking.append((0.78 if added_required else 0.45,
                                       "%s: signature of %s changed" % (f.path, name)))
            for l in removed:
                m = EXPORT_REMOVED.match(l)
                if m and not any(re.match(r"^\s*export\b.*\b%s\b" % re.escape(m.group(1)), a) for a in added):
                    F.breaking.append((0.72, "%s: export %s removed" % (f.path, m.group(1))))
                    break
            if PUBLIC_API_PATH.search(f.path) and removed:  # boost only this file's findings, not earlier files'
                F.breaking[first:] = [(min(p + 0.1, 0.92), e) for p, e in F.breaking[first:]]
        if cat == "code":
            lines = added
            for i, l in enumerate(lines):
                m = LOOP.match(l)
                if not m:
                    continue
                indent = len(m.group(1))
                for nxt in lines[i + 1:i + 8]:
                    if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= indent:
                        break
                    if QUERY_CALL.search(nxt):
                        F.perf.append("%s query or request inside a loop" % _loc(f, i))
                        break
            for i, l in enumerate(added):
                if SIDE_EFFECT.search(_strip_strings(l)):
                    F.side_effects.append(_loc(f, i))
                    break
        if cat == "manifest" and f.status != "file":
            def versions(lines):
                out = {}
                for l in lines:
                    m = VERSION_PKG_JSON.search(l) or VERSION_REQ.search(l.strip())
                    if m:
                        out[m.group(1)] = int(m.group(2))
                return out
            before, after = versions(removed), versions(added)
            for pkg, v in after.items():
                if pkg in before and before[pkg] != v:
                    F.majors.append("%s %d -> %d" % (pkg, before[pkg], v))
    for p, e in sorted(F.security, reverse=True)[:2]:
        F.fact("hint", e, "security")
    for p, e in sorted(F.breaking, reverse=True)[:2]:
        F.fact("hint", "possible breaking change: " + e, "breaking")
    if F.migration.get("destructive"):
        F.fact("hint", "destructive migration (drop, rename or type change) in " + F.migration["destructive"], "migration_destructive")
    if F.migration.get("backfill"):
        F.fact("hint", "data backfill or rewrite in " + F.migration["backfill"], "migration_backfill")
    if F.migration.get("orm_models") and "has_migration" not in F.comp:
        F.fact("hint", "data model changed in %s without a migration file" % F.migration["orm_models"], "orm_no_migration")
    for e in F.perf[:2]:
        F.fact("hint", e, "perf")
    if F.side_effects:
        F.fact("hint", "external side effects added at " + F.side_effects[0], "side_effects")
    if F.majors:
        F.fact("hint", "major version bump: " + ", ".join(F.majors[:3]), "major_bump")


# ---------------------------------------------------------------- deciders

def _ai_free(F, item):
    if "inert" in F.comp:
        return True
    return not F.ai_present and not (item.text and re.search(
        r"(?i)\b(llm|gpt|claude|gemini|prompt|model|agent|embedding|rag|fine-?tun\w*|training|inference|cuda|gpu|"
        r"hallucinat\w*|tokens?)\b", item.text))


def _text_scores(table, text):
    return {k: len(rx.findall(text)) for k, rx in table.items()}


def decide(preset, F, item, routes=None, router=None):
    """Return an Answer for one preset, or None if System 1 has nothing useful."""
    comp = F.comp
    text = "\n".join(t for t in (item.text, item.commit_message) if t)
    has_files = bool(item.files)
    inert = "inert" in comp
    ai_free = _ai_free(F, item)
    strong_secret = [s for s in F.secrets if s[3] >= 0.85]

    if preset == "secret_leak":
        if F.secrets:
            best = max(F.secrets, key=lambda s: s[3])
            ev = ["%s %s at %s" % (k, m, l) for k, m, l, _ in F.secrets[:3]]
            return V(best[3], EXACT if best[3] >= 0.85 else HINT, ev)
        if has_files or text:
            if F.scan_truncated:
                return V(0.1, HINT, ["the change is too large to scan fully for credentials"])
            return V(0.04, HINT if F.secret_words else EXACT,
                     ["no credential patterns in the changed lines"] if not F.secret_words else
                     ["secret-like words present but no credential pattern matched"])
        return None

    if preset == "security_relevant":
        if strong_secret:
            return V(0.95, EXACT, ["credential in the change"])
        if F.security:
            p, e = max(F.security)
            return V(p, HINT, [e for _, e in sorted(F.security, reverse=True)[:3]])
        if inert or comp & {"tests_only", "deps_only"} and not F.majors:
            return V(0.05 if inert else 0.12, EXACT if inert else HINT, ["no code or security-sensitive files changed"] if inert else None)
        if has_files:
            return V(0.2, HINT)
        return None

    if preset == "task_type":
        if "docs_only" in comp:
            return C({"docs": 0.93, "_rest": 0.07}, EXACT, ["only documentation changed"])
        if "tests_only" in comp:
            return C({"test": 0.9, "_rest": 0.1}, EXACT, ["only tests changed"])
        if "deps_only" in comp:
            return C({"dependency": 0.92, "_rest": 0.08}, EXACT, ["only dependency files changed"])
        if "ci_only" in comp:
            return C({"build_ci": 0.9, "_rest": 0.1}, EXACT, ["only CI configuration changed"])
        if "eval_only" in comp:
            return C({"eval": 0.86, "_rest": 0.14}, EXACT, ["only eval files changed"])
        if "prompt_only" in comp:
            return C({"prompt_engineering": 0.86, "_rest": 0.14}, EXACT, ["only prompt files changed"])
        if "style_only" in comp:
            return C({"ui_ux": 0.75, "_rest": 0.25}, HINT)
        scores = _text_scores(TASK_WORDS, text) if text else {}
        if F.migration.get("additive") or F.migration.get("destructive"):
            scores["data_migration"] = scores.get("data_migration", 0) + 2
        if F.ai_change.get("training_code") and F.ml_present:
            scores["model_training"] = scores.get("model_training", 0) + 2
        if F.ai_change.get("prompt"):
            scores["prompt_engineering"] = scores.get("prompt_engineering", 0) + 1
        d = from_scores(scores, smooth=0.15)
        return C(d, HINT) if d else None

    if preset == "commit_type":
        task = decide("task_type", F, item)
        if task and task.strength == EXACT:
            top = max((k for k in task.probs if k != "_rest"), key=task.probs.get)
            ct = COMMIT_FROM_TASK.get(top, "chore")
            if top == "dependency":
                return C({"build": 0.55, "chore": 0.4, "_rest": 0.05}, EXACT)
            return C({ct: 0.88, "_rest": 0.12}, EXACT)
        if task:
            d = {}
            for k, v in task.probs.items():
                if k == "_rest":
                    d["_rest"] = d.get("_rest", 0) + v
                else:
                    c = COMMIT_FROM_TASK.get(k, "chore")
                    d[c] = d.get(c, 0) + v
            return C(d, HINT)
        return None

    if preset == "language":
        if F.langs:
            total = float(sum(F.langs.values()))
            if len(F.langs) == 1:
                lang = next(iter(F.langs))
                return C({lang: 0.95, "_rest": 0.05}, EXACT, ["file extensions"])
            d = {k: 0.9 * v / total for k, v in F.langs.most_common(4)}
            d["_rest"] = 0.1
            return C(d, HINT)
        if has_files and "docs_only" in comp:
            return C({"other": 0.85, "_rest": 0.15}, EXACT, ["documentation only"])
        if has_files and F.config_lines:
            return C({"config_yaml_json": 0.85, "_rest": 0.15}, EXACT, ["configuration files only"])
        if text:
            if re.search(r"Traceback \(most recent call last\)|File \".+\.py\", line \d+", text):
                return C({"python": 0.92, "_rest": 0.08}, EXACT, ["Python traceback"])
            if re.search(r"\.tsx?:\d+|error TS\d{4}", text):
                return C({"typescript": 0.85, "_rest": 0.15}, HINT)
            if re.search(r"at .+\.(m?js|jsx):\d+|node_modules|Uncaught \w+Error", text):
                return C({"javascript": 0.7, "typescript": 0.2, "_rest": 0.1}, HINT)
            if re.search(r"Exception in thread|at [\w.$]+\([\w]+\.java:\d+\)", text):
                return C({"java": 0.85, "kotlin": 0.08, "_rest": 0.07}, HINT)
            if re.search(r"panic: |goroutine \d+ \[", text):
                return C({"go": 0.92, "_rest": 0.08}, EXACT, ["Go panic trace"])
            if re.search(r"thread '.+' panicked at|error\[E\d{4}\]", text):
                return C({"rust": 0.92, "_rest": 0.08}, EXACT, ["Rust panic or compiler error"])
        return None

    if preset == "domain":
        if "docs_only" in comp:
            return C({"docs": 0.85, "_rest": 0.15}, EXACT)
        corpus = " ".join(list(F.cats)) + "\n" + text[:20000] + "\n" + "\n".join(
            l for f in _code_files(item) for l in f.added[:300])[:40000]
        scores = {d: min(len(rx.findall(corpus)), 8) for d, rx in DOMAIN_RX.items()}
        if F.llm_present:
            scores["llm_app"] = scores.get("llm_app", 0) + 3
        if F.agent.get("tools"):
            scores["ai_agents"] = scores.get("ai_agents", 0) + 4
        if F.ml_present:
            scores["ml_classic"] = scores.get("ml_classic", 0) + 2
        d = from_scores(scores, smooth=0.2, top=4)
        return C(d, HINT) if d else None

    if preset == "issue_type":
        if not text:
            return None
        scores = _text_scores(ISSUE_WORDS, text)
        if F.err_general or F.err_ai:
            scores["bug"] = scores.get("bug", 0) + 3
        if len(text.split()) < 4:
            scores["duplicate_or_noise"] = scores.get("duplicate_or_noise", 0) + 2
        d = from_scores(scores, smooth=0.12)
        return C(d, HINT) if d else None

    if preset == "error_category":
        matches = F.err_general
        if F.err_ai and (not matches or F.err_ai[0][0] <= matches[0][0]):
            return C({"ai_model": 0.86, "_rest": 0.14}, EXACT, ["AI failure signature: " + F.err_ai[0][1]])
        if matches:
            cats = [c for _, c in matches]
            first = cats[0]
            distinct = list(dict.fromkeys(cats))
            if len(distinct) == 1:
                return C({first: 0.9, "_rest": 0.1}, EXACT, ["error signature: " + first])
            d = {first: 0.62}
            for c in distinct[1:3]:
                d[c] = 0.25 / len(distinct[1:3])
            d["_rest"] = 0.13
            return C(d, HINT, ["first error: " + first])
        return None

    if preset == "ai_failure":
        if F.err_ai:
            cats = [c for _, c in F.err_ai]
            distinct = list(dict.fromkeys(cats))
            if len(distinct) == 1:
                return C({distinct[0]: 0.9, "_rest": 0.1}, EXACT, ["signature: " + distinct[0]])
            d = {distinct[0]: 0.65}
            for c in distinct[1:3]:
                d[c] = 0.22 / len(distinct[1:3])
            d["_rest"] = 0.13
            return C(d, HINT)
        if F.err_general and ai_free:
            return C({"not_ai_related": 0.88, "_rest": 0.12}, EXACT, ["general error with no AI involvement"])
        return None

    if preset == "ai_change_type":
        if ai_free:
            return C({"none": 0.93, "_rest": 0.07}, EXACT, ["no AI code involved"])
        if "prompt_only" in comp:
            return C({"prompt": 0.9, "_rest": 0.1}, EXACT)
        if "eval_only" in comp:
            return C({"eval_suite": 0.9, "_rest": 0.1}, EXACT)
        d = from_scores(dict(F.ai_change), smooth=0.15, top=4)
        if d:
            return C(d, HINT)
        return C({"none": 0.5, "_rest": 0.5}, HINT)

    if preset == "ai_behavior_change":
        if ai_free:
            return V(0.03, EXACT, ["no AI code involved"])
        strong = ("prompt", "model_swap", "model_params")
        if "prompt_only" in comp or (F.llm_present and any(F.ai_change.get(k, 0) >= 1 for k in strong)):
            ev = [k.replace("_", " ") for k in strong if F.ai_change.get(k)]
            return V(0.93, EXACT, ["changed: " + ", ".join(ev or ["prompt files"])])
        if any(F.ai_change.get(k) for k in BEHAVIOR_TYPES):
            return V(0.8, HINT)
        return V(0.25, HINT)

    if preset == "needs_evals":
        if ai_free:
            return V(0.04, EXACT, ["no AI code involved"])
        b = decide("ai_behavior_change", F, item)
        if b and b.p >= 0.8:
            return V(0.88, HINT, ["model or prompt behavior changes"])
        return V(0.45, HINT)

    if preset == "eval_needed":
        if ai_free:
            return C({"none": 0.9, "_rest": 0.1}, EXACT)
        ch = F.ai_change
        if ch.get("agent_tools") and F.agent.get("untrusted"):
            return C({"red_team": 0.5, "offline_eval_set": 0.3, "_rest": 0.2}, HINT)
        if ch.get("model_swap"):
            return C({"offline_eval_set": 0.55, "ab_test": 0.25, "_rest": 0.2}, HINT)
        if ch.get("prompt") or ch.get("model_params"):
            return C({"offline_eval_set": 0.45, "golden_examples": 0.35, "_rest": 0.2}, HINT)
        if ch.get("training_code") or F.ml_present:
            return C({"offline_eval_set": 0.7, "_rest": 0.3}, HINT)
        return None

    if preset in ("prompt_injection_exposure", "excessive_agency", "unvalidated_model_output", "pii_in_ai_data"):
        if ai_free or (has_files and not F.llm_present and not text):
            return V(0.03, EXACT, ["no LLM calls involved"])
        a = F.agent
        if not a:
            return None
        dangerous = [c for c in a.get("caps", []) if c in DANGEROUS_CAPS]
        if preset == "prompt_injection_exposure":
            if a["untrusted"] and (a["tools"] or a["sink"] or dangerous):
                return V(0.85, HINT, ["untrusted %s reaches a model with tools or actions" % ", ".join(a["untrusted"][:2])])
            if a["untrusted"]:
                return V(0.45, HINT)
            return V(0.12, HINT)
        if preset == "excessive_agency":
            if dangerous and not a["approval"]:
                return V(0.85, HINT, ["%s with no approval step" % ", ".join(dangerous)])
            if dangerous:
                return V(0.35, HINT, ["%s behind an approval step" % ", ".join(dangerous)])
            return V(0.08 if a["tools"] else 0.05, HINT)
        if preset == "unvalidated_model_output":
            if a["sink"] and not a["validated"]:
                return V(0.85, HINT, ["model output used in a command, query or message without validation"])
            if dangerous and not a["validated"]:
                return V(0.55, HINT)
            if a["validated"]:
                return V(0.2, HINT)
            return V(0.1, HINT)
        if preset == "pii_in_ai_data":
            pii, logs = F.pii or (False, False)
            if pii and logs:
                return V(0.82, HINT, ["personal fields near model calls and prompts are logged"])
            if pii:
                return V(0.65, HINT)
            return V(0.15, HINT)

    if preset == "agent_autonomy_risk":
        if ai_free or (has_files and not F.llm_present):
            return S({1: 0.93, 2: 0.05, 3: 0.02}, EXACT, ["no agent or tools"])
        a = F.agent
        if not a:
            return None
        caps = a.get("caps", [])
        dangerous = [c for c in caps if c in DANGEROUS_CAPS]
        if not a["tools"] and not dangerous:
            return S(peaked(1, mass=0.75), HINT)
        if dangerous:
            level = 4 if a["approval"] else 5
        elif "file_write" in caps:
            level = 3
        else:
            level = 2
        return S(peaked(level, mass=0.65), HINT, ["tools: " + (", ".join(caps) or "read-only")])

    if preset == "ai_cost_impact":
        if ai_free or (has_files and not F.llm_present and not F.ml_present):
            return S({1: 0.93, 2: 0.05, 3: 0.02}, EXACT, ["no model calls involved"])
        c = F.cost or {}
        if c.get("unbounded"):
            return S(peaked(5, mass=0.7), HINT, ["model calls in an uncapped loop"])
        if c.get("loop"):
            return S(peaked(4, mass=0.55), HINT, ["model calls inside a loop"])
        if c.get("removed_limit"):
            return S(peaked(3, mass=0.55), HINT, ["output token limit removed"])
        return S(peaked(2, mass=0.5), HINT)

    if preset == "model_unpinned":
        if F.model_ids:
            unp = [m for m, pinned, _ in F.model_ids if not pinned]
            if any("latest" in m.lower() for m in unp):
                return V(0.95, EXACT, ["floating alias: " + ", ".join(m for m in unp if "latest" in m.lower())[:80]])
            if unp:
                return V(0.8, HINT, ["no snapshot date: " + ", ".join(unp[:3])])
            return V(0.07, HINT, ["pinned: " + ", ".join(m for m, _, _ in F.model_ids[:3])])
        if F.hf_unpinned:
            return V(0.6, HINT, ["from_pretrained without revision in " + F.hf_unpinned[0]])
        if ai_free:
            return V(0.03, EXACT)
        if has_files:
            return V(0.15, HINT, ["no model ids or pretrained checkpoints referenced"])
        return None

    if preset == "ml_data_leakage":
        if not F.ml_present:
            if ai_free or has_files:
                return V(0.03, EXACT if (ai_free or not F.ai_present) else HINT, ["no ML training code"])
            return None
        if F.leak:
            p = 1.0
            for q, _ in F.leak:
                p *= (1 - q)
            return V(min(1 - p, 0.96), HINT, [e for _, e in F.leak[:3]])
        return V(0.12, HINT)

    if preset == "nondeterministic":
        if inert:
            return V(0.03, EXACT)
        if text and not has_files:
            flaky = len(FLAKY_WORDS.findall(text))
            det = len(DETERMINISTIC_WORDS.findall(text))
            if flaky or det:
                return V(0.8 if flaky > det else 0.2, HINT)
        if F.nondet:
            return V(F.nondet[0], HINT, [F.nondet[1]])
        return None

    if preset == "is_flaky":
        if not text:
            return None
        flaky = len(FLAKY_WORDS.findall(text))
        det = len(DETERMINISTIC_WORDS.findall(text))
        if F.err_general and any(c == "concurrency" for _, c in F.err_general):
            flaky += 2
        if flaky > det:
            return V(0.8, HINT)
        if det > flaky:
            return V(0.15, HINT)
        return V(0.3, HINT)

    if preset == "production_impact":
        if not text:
            return None
        prod = len(PROD_WORDS.findall(text))
        nonprod = len(NONPROD_WORDS.findall(text))
        if prod > nonprod:
            return V(0.82, HINT)
        if nonprod > prod:
            return V(0.18, HINT)
        return V(0.4, HINT)

    if preset == "severity":
        if not text:
            return None
        d = from_scores(_text_scores(SEVERITY_WORDS, text), smooth=0.15)
        return C(d, HINT) if d else C({"sev3_minor": 0.4, "sev2_major": 0.3, "_rest": 0.3}, HINT)

    if preset in ("needs_clarification", "clarity"):
        if not text or has_files:
            return None
        words = len(text.split())
        clear = len(CLEAR_WORDS.findall(text))
        vague = len(VAGUE_WORDS.findall(text))
        unclear = 0.8 if (words < 6 or vague > clear) else (0.45 if words < 15 and clear == 0 else 0.15)
        if preset == "needs_clarification":
            return V(unclear, HINT)
        level = 1 if unclear >= 0.8 else (3 if unclear >= 0.45 else 4)
        return S(peaked(level, mass=0.55), HINT)

    if preset == "model_tier":
        corpus = text[:5000]
        if not corpus and not has_files:
            return None
        h = len(HAIKU_WORDS.findall(corpus))
        o = len(OPUS_WORDS.findall(corpus))
        if inert or "deps_only" in comp:
            return C({"haiku": 0.75, "sonnet": 0.22, "_rest": 0.03}, HINT)
        if F.security or F.agent.get("caps"):
            o += 1
        if o > h:
            return C({"opus": 0.55, "sonnet": 0.38, "_rest": 0.07}, HINT)
        if h > o:
            return C({"haiku": 0.55, "sonnet": 0.4, "_rest": 0.05}, HINT)
        return C({"sonnet": 0.6, "opus": 0.2, "haiku": 0.15, "_rest": 0.05}, HINT)

    if preset == "effort":
        if text and not has_files:
            scores = {str(k): len(rx.findall(text)) for k, rx in EFFORT_WORDS.items()}
            if any(scores.values()):
                lvl = max(scores, key=lambda k: (scores[k], int(k)))
                return S(peaked(int(lvl), mass=0.45), HINT)
            return None
        if has_files:
            n = F.lines_changed
            lvl = 1 if n < 20 else 2 if n < 100 else 3 if n < 400 else 4 if n < 1500 else 5
            return S(peaked(lvl, mass=0.5), HINT)
        return None

    if preset == "risk":
        if not has_files:
            return None
        if inert:
            return S({1: 0.88, 2: 0.1, 3: 0.02}, EXACT, ["documentation, styles or assets only"])
        if "tests_only" in comp:
            return S({1: 0.55, 2: 0.38, 3: 0.07}, EXACT, ["tests only"])
        levels = []
        if strong_secret:
            levels.append((5, "credential in the change"))
        if F.migration.get("destructive"):
            levels.append((5, "destructive migration"))
        elif "has_migration" in comp:
            levels.append((4, "schema migration"))
        if F.security and max(F.security)[0] >= 0.8:
            levels.append((4, "security-sensitive code"))
        elif F.security:
            levels.append((4, "sensitive path"))
        if F.breaking and max(F.breaking)[0] >= 0.7:
            levels.append((4, "public signature change"))
        if F.agent.get("caps") and [c for c in F.agent["caps"] if c in DANGEROUS_CAPS]:
            levels.append((4, "agent with consequential tools"))
        if F.ai_change.get("prompt") or F.ai_change.get("model_swap") or F.ai_change.get("model_params"):
            levels.append((3, "model or prompt behavior change"))
        if F.leak and max(F.leak)[0] >= 0.8:
            levels.append((4, "likely train/test leakage"))
        if "deps_only" in comp:
            levels.append((3 if F.majors else 2, "dependency change"))
        if "ci_only" in comp:
            levels.append((3, "CI change"))
        n = F.lines_changed
        levels.append((2 if n < 50 else 3 if n < 400 else 4, "size of the change"))
        lvl, why = max(levels)
        return S(peaked(lvl, mass=0.55), HINT, [why])

    if preset == "breaking_change":
        if not has_files:
            return None
        if inert or "tests_only" in comp:
            return V(0.03, EXACT, ["no runtime code changed"])
        if "deps_only" in comp:
            return V(0.35 if F.majors else 0.08, HINT, F.majors[:2] or None)
        if F.migration.get("destructive"):
            return V(0.7, HINT, ["destructive migration"])
        if F.breaking:
            p, e = max(F.breaking)
            return V(p, HINT, [x for _, x in sorted(F.breaking, reverse=True)[:3]])
        if F.ai_change.get("model_swap"):
            return V(0.5, HINT, ["model swapped"])
        return V(0.2, HINT)

    if preset == "needs_tests":
        if not has_files:
            return None
        if inert:
            return V(0.03, EXACT, ["no runtime code changed"])
        if "tests_only" in comp:
            return V(0.05, EXACT, ["the change is tests"])
        if "deps_only" in comp:
            return V(0.25, HINT)
        if F.ai_change and F.llm_present and "code" not in set(F.cats.values()):
            return V(0.4, HINT, ["AI behavior is checked with evals more than unit tests"])
        if "has_tests" in comp:
            return V(0.3, HINT, ["tests changed alongside the code"])
        if "has_code" in comp:
            return V(0.75, HINT, ["code changed without test changes"])
        return V(0.3, HINT)

    if preset == "migration_required":
        if "has_migration" in comp:
            return V(0.95, EXACT, ["migration files in the change"])
        if not has_files:
            return None
        if F.migration.get("orm_models"):
            return V(0.7, HINT, ["data model changed without a migration: " + F.migration["orm_models"]])
        if inert or comp & {"tests_only", "ci_only"}:
            return V(0.03, EXACT, ["no data model or schema files changed"])
        return V(0.07, HINT)

    if preset == "rollback_safe":
        if not has_files:
            return None
        if inert or "tests_only" in comp:
            return V(0.97, EXACT, ["no runtime or data changes"])
        if F.migration.get("destructive"):
            return V(0.12, HINT, ["destructive migration in " + F.migration["destructive"]])
        if F.migration.get("backfill"):
            return V(0.3, HINT, ["data backfill in " + F.migration["backfill"]])
        if "has_migration" in comp:
            return V(0.7, HINT, ["additive migration"])
        if F.side_effects:
            return V(0.55, HINT, ["external side effects at " + F.side_effects[0]])
        return V(0.85, HINT)

    if preset == "perf_sensitive":
        if not has_files:
            return None
        if inert or "tests_only" in comp:
            return V(0.03, EXACT)
        if F.perf:
            return V(0.72, HINT, F.perf[:2])
        if any(re.search(r"(?i)cache|index|pool|batch|limit", p) for p in F.cats):
            return V(0.5, HINT)
        return V(0.2, HINT)

    if preset == "change_scope":
        if not has_files:
            return None
        n = len(item.files)
        dirs = next((int(c.split(":")[1]) for c in comp if c.startswith("dirs:")), 1)
        if "docs_only" in comp and n <= 3:
            return C({"local": 0.85, "_rest": 0.15}, EXACT)
        if n == 1:
            return C({"local": 0.7, "module": 0.22, "_rest": 0.08}, HINT)
        if dirs == 1:
            return C({"module": 0.6, "local": 0.25, "_rest": 0.15}, HINT)
        if dirs <= 3:
            return C({"cross_module": 0.55, "module": 0.3, "_rest": 0.15}, HINT)
        return C({"system_wide": 0.55, "cross_module": 0.35, "_rest": 0.1}, HINT)

    if preset == "test_type_needed":
        if inert and "style_only" not in comp:
            return C({"none": 0.88, "_rest": 0.12}, EXACT)
        if F.ai_change and F.llm_present:
            return C({"eval": 0.65, "unit": 0.15, "_rest": 0.2}, HINT)
        exts = [_ext(p) for p in F.cats]
        if any(e in (".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss") for e in exts):
            return C({"unit": 0.4, "e2e": 0.35, "_rest": 0.25}, HINT)
        if F.security:
            return C({"security": 0.4, "unit": 0.3, "_rest": 0.3}, HINT)
        if F.perf:
            return C({"performance": 0.45, "unit": 0.25, "_rest": 0.3}, HINT)
        if any(PUBLIC_API_PATH.search(p) for p in F.cats):
            return C({"integration": 0.5, "unit": 0.3, "_rest": 0.2}, HINT)
        return None

    if preset == "review_verdict":
        if strong_secret:
            return C({"request_changes": 0.92, "needs_discussion": 0.05, "_rest": 0.03}, EXACT, ["credential committed"])
        if inert and not F.secrets:
            return C({"approve": 0.85, "needs_discussion": 0.1, "_rest": 0.05}, EXACT, ["documentation, styles or assets only"])
        risky = (F.leak and max(F.leak)[0] >= 0.8) or (F.security and max(F.security)[0] >= 0.85) or \
            (F.agent.get("caps") and not F.agent.get("approval") and F.agent.get("untrusted"))
        if risky:
            return C({"request_changes": 0.7, "needs_discussion": 0.22, "_rest": 0.08}, HINT)
        if "tests_only" in comp:
            return C({"approve": 0.7, "needs_discussion": 0.2, "_rest": 0.1}, HINT)
        return C({"needs_discussion": 0.4, "approve": 0.35, "request_changes": 0.25}, HINT)

    if preset == "route" and router is not None and routes is not None:
        query = text or " ".join(F.cats)
        if not query.strip():
            return None
        res = router.route(query, routes)
        if res is None:
            return None
        probs, exact, best = res
        return C(probs, EXACT if exact else HINT, ["named in the request: " + best] if exact else None)

    return None


def analyze(item, wanted, routes=None, router=None):
    """Run every analyzer once, then answer the wanted presets that System 1 can speak to."""
    F = Facts()
    if item.error or item.kind == "empty":
        return F, {}
    _files(item, F)
    _secrets(item, F)
    _errors(item, F)
    _ai(item, F)
    _code(item, F)
    answers = {}
    for preset in wanted:
        try:
            ans = decide(preset, F, item, routes=routes, router=router)
        except Exception:
            ans = None
        if ans is not None:
            answers[preset] = ans
    return F, answers
