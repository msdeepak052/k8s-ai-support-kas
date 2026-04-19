# k8s-ai-support

**Production-grade AI-powered Kubernetes troubleshooting system** with CLI and MCP server support.

Diagnose pod failures, deployment issues, network problems, and more — using natural language. Works with VS Code, Cursor, Kiro, Claude Desktop, and any MCP-compatible IDE.

```
User: "Why is my nginx pod in CrashLoopBackOff?"

Agent: Fetches kubectl describe + logs → summarizes (842 tokens, 95% reduction) →
       queries LLM → returns structured diagnosis with kubectl commands
```

---

## Features

- **Multi-LLM**: OpenAI, Google Gemini, Anthropic Claude, or local Ollama
- **MCP Server**: Attach to VS Code, Cursor, Kiro, Claude Desktop
- **CLI**: Interactive REPL or one-shot queries
- **RAG**: Local Kubernetes docs — no external API needed for embeddings
- **Read-Only Safety**: Hard-enforced blocklist — cannot mutate your cluster
- **Token Efficient**: 95% token reduction via structured summarization
- **Async Parallel**: Fetches multiple resources simultaneously
- **Structured Output**: JSON/YAML/table output formats
- **Debug Mode**: `--debug` flag traces the full internal data-flow to stderr — see exactly what kubectl fetches, what logs arrive, what the LLM receives

---

## Quick Start

### 1. Install

**One-liner — works with or without uv**

```bash
git clone https://github.com/msdeepak052/k8s-ai-support-kas.git
cd k8s-ai-support-kas
bash install.sh
```

The script auto-detects your environment:

| Situation | What the script does |
|-----------|---------------------|
| `uv` installed | Uses `uv tool install` — `kas` registered globally, Python 3.12 managed automatically |
| `uv` missing, Python 3.11–3.13 found | Creates `.venv`, installs deps, creates `~/.local/bin/kas` wrapper |
| Neither found | Prints instructions to install uv or Python, then exits cleanly |

After install, both commands work from anywhere — no `uv run` prefix needed:

```bash
kas "why is my nginx pod crashing?"
k8s-ai-support "why is my nginx pod crashing?"
```

**After a `git pull` — pick up code changes:**

```bash
bash install.sh --update
```

**Uninstall:**

```bash
bash install.sh --uninstall
```

**Manual install (uv, if you prefer)**

```bash
# Install uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# Then
uv tool install --python 3.12 --editable ".[all]"
```

### 2. Configure

```bash
# Copy and edit environment file
cp .env.example .env
nano .env

# Or set directly
export OPENAI_API_KEY=sk-...
export K8S_AI_PROVIDER=openai
export K8S_AI_MODEL=gpt-4o-mini
```

### 3. Run

`kas` and `k8s-ai-support` are identical — use whichever you prefer.

> **Query must come first**, before any flags. `kas "query" -n namespace` is correct; `kas -n namespace "query"` will not route correctly.

```bash
# Single query — natural language, query always first
kas "why is my nginx pod crashing?"

# With namespace and resource
kas "pod failing" -n production -r nginx-pod-abc123 -t pod

# Explicit subcommand also works
kas diagnose "deployment not ready" -n production -t deployment

# Interactive REPL mode
kas --interactive

# JSON output
kas "deployment not scaling" -n production -o json

# Debug mode — trace every internal step on stderr (see Debug Mode section)
kas "why is crash-loop-pod crashing?" -n kas-test --debug

# Debug + verbose (also prints execution metadata table at the end)
kas "why is crash-loop-pod crashing?" -n kas-test --debug --verbose

# Check cluster connectivity and LLM config
kas check

# Start MCP server (for IDE integration)
kas mcp
```

### CLI Flags Reference

| Flag | Short | Description |
|------|-------|-------------|
| `--namespace` | `-n` | Kubernetes namespace (default: `default`) |
| `--resource` | `-r` | Specific resource name — skips auto-detection |
| `--type` | `-t` | Resource type: `pod`, `deployment`, `service`, `node`, `pvc` |
| `--output` | `-o` | Output format: `table` (default), `json`, `yaml` |
| `--debug` | `-d` | Enable debug logging — traces internal data-flow to stderr |
| `--verbose` | `-v` | Show execution metadata table after diagnosis |
| `--provider` | `-p` | Override LLM provider: `openai`, `gemini`, `claude`, `ollama` |
| `--model` | `-m` | Override LLM model name |
| `--interactive` | `-i` | Start interactive REPL mode |

---

## Debug Mode

The `--debug` flag (`-d`) enables full internal tracing on **stderr**, keeping the diagnosis output on **stdout** cleanly separated. Use it when a diagnosis is generic, a fetch is failing silently, or you want to see exactly what the agent is doing.

```bash
kas "why is crash-loop-pod crashing?" -n kas-test --debug
```

Sample output (stderr):
```
18:21:16 [DEBUG] src.agent.nodes: [ROUTER] query='why is crash-loop-pod crashing?'
18:21:16 [DEBUG] src.agent.nodes: [ROUTER] cluster_keywords_match=True, cluster_reachable=True
18:21:16 [DEBUG] src.agent.nodes: [FETCH]  name-like tokens in query: ['crash-loop-pod']
18:21:16 [DEBUG] src.agent.nodes: [FETCH]  auto-detected resource_name='crash-loop-pod' from query
18:21:16 [DEBUG] src.agent.nodes: [FETCH]  planned tasks: ['events', 'pods', 'logs', 'prev_logs']
18:21:17 [DEBUG] src.agent.nodes: [FETCH]  logs               → OK       (159 chars stdout)
18:21:18 [DEBUG] src.agent.nodes: [FETCH]  log[crash-loop-pod] = 159 chars
18:21:18 [DEBUG] src.agent.nodes: [SUMMARIZE] pod=crash-loop-pod  log=159 chars  prev_log=159 chars
18:21:43 [DEBUG] src.tools.rag_tools: [RAG]  chunk: title='CrashLoopBackOff Troubleshooting' relevance=0.750
18:21:43 [DEBUG] src.agent.nodes: [ANALYZE] mode=cluster+rag, prompt=7420 chars
18:21:50 [DEBUG] src.agent.nodes: [ANALYZE] JSON parse success=True
```

### What Each Section Shows

| Section | What It Traces |
|---------|---------------|
| `[ROUTER]` | Query analysis, keyword matches, cluster probe result, chosen route |
| `[FETCH]` | Resource name detection, planned kubectl tasks, per-task OK/FAIL with char counts |
| `[KUBECTL]` | Exact kubectl command, return code, stdout/stderr sizes |
| `[KUBECTL-LOGS]` | Pod log fetches — pod name, previous flag, result size |
| `[SUMMARIZE]` | Per-pod log sizes going into summarizer, token count coming out |
| `[RAG]` | Retrieval mode (semantic vs keyword fallback), per-chunk relevance scores |
| `[ANALYZE]` | Prompt mode, prompt size, LLM response size, JSON parse success |

### Filtering Debug Output

```bash
# Did the right logs get fetched?
kas "query" -n ns --debug 2>&1 | grep "log\["

# What kubectl commands ran and what did they return?
kas "query" -n ns --debug 2>&1 | grep -E "\[KUBECTL\]|\[KUBECTL-LOGS\]"

# Was the resource name auto-detected from the query?
kas "query" -n ns --debug 2>&1 | grep "\[FETCH\]"

# What route did the agent take?
kas "query" -n ns --debug 2>&1 | grep "\[ROUTER\]"

# Did RAG find relevant docs? What relevance scores?
kas "query" -n ns --debug 2>&1 | grep "\[RAG\]"

# How big was the prompt sent to the LLM?
kas "query" -n ns --debug 2>&1 | grep "\[ANALYZE\]"

# Save debug trace to a file for sharing
kas "query" -n ns --debug 2>/tmp/kas-debug.log
```

### Debug vs Verbose

| Flag | What It Does |
|------|-------------|
| _(neither)_ | Silent — only warnings/errors printed |
| `--verbose` | Adds execution metadata table (steps, tokens, timing) after diagnosis |
| `--debug` | Full internal trace on stderr, spinner disabled |
| `--debug --verbose` | Both: trace on stderr + metadata table after diagnosis |

---

## Architecture

![k8s-ai-support Architecture](./images/Architecture.png)

**The LLM runs on the provider's cloud** (OpenAI/Gemini/Anthropic). Only inference calls go to the cloud. Everything else (kubectl, RAG, summarization) runs locally.

---

## Token Optimization

Raw kubectl output can be 15KB+. We compress it to ~800 bytes before sending to the LLM:

```python
# Raw pod JSON: ~15,000 bytes (≈ 4,000 tokens)
# Structured summary: ~800 bytes (≈ 200 tokens) — 95% reduction!
{
  "resource_type": "Pod",
  "name": "nginx-xxx",
  "phase": "Running",
  "container_statuses": [{
    "name": "nginx",
    "restart_count": 7,
    "state": "waiting",
    "reason": "CrashLoopBackOff",
    "last_termination_exit_code": 1,
    "last_termination_log_snippet": "Error: port 8080 already in use"
  }],
  "events": [{"type": "Warning", "reason": "BackOff", "count": 15}]
}
```

---

## LLM Providers

| Provider | Model | Cost | Speed | Notes |
|----------|-------|------|-------|-------|
| OpenAI | gpt-4o-mini | ~$0.0002/query | Fast | Best default choice |
| OpenAI | gpt-4o | ~$0.003/query | Fast | Higher accuracy |
| Gemini | gemini-1.5-flash | ~$0.0001/query | Very Fast | Free tier available |
| Gemini | gemini-1.5-pro | ~$0.002/query | Fast | Better reasoning |
| Claude | claude-3-5-haiku | ~$0.0003/query | Fast | Good at structured output |
| Claude | claude-3-5-sonnet | ~$0.004/query | Medium | Best reasoning |
| Ollama | llama3.1 | Free | Slow | Requires 8GB+ RAM |

Switch providers:
```bash
export K8S_AI_PROVIDER=gemini
export K8S_AI_MODEL=gemini-1.5-flash
export GEMINI_API_KEY=AIza...
```

---

## MCP Server (IDE Integration)

See [`docs/vscode_integration.md`](docs/vscode_integration.md) for full setup.

**VS Code quick config** (`settings.json`):
```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "${env:OPENAI_API_KEY}",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini"
      }
    }
  }
}
```

**Available MCP Tools:**
- `k8s_diagnose` — Full AI diagnosis
- `k8s_get_resources` — List any K8s resource
- `k8s_get_logs` — Fetch pod logs
- `k8s_describe` — Describe a resource
- `k8s_get_events` — Get cluster events

---

## Safety

**All kubectl operations are strictly read-only.** The agent cannot mutate your cluster.

Hard-blocked commands:
```
delete, patch, apply, edit, scale, exec, cp, rollout undo,
drain, cordon, uncordon, taint, label, annotate, create,
replace, expose, run, set, autoscale
```

Shell injection is also blocked: `;`, `|`, `&`, `` ` ``, `$`

The agent outputs kubectl commands for you to run manually.

---

## Output Example

```
======================================================================
  K8S-AI-SUPPORT DIAGNOSIS
======================================================================
Severity    : [HIGH]
Category    : crashloop
Confidence  : 87%
Root Cause  : Container exits with exit code 1 due to missing env var DATABASE_URL
Affected    : pod/nginx-abc123

ANALYSIS:
  The container is in CrashLoopBackOff with 7 restarts (exponential
  backoff). Exit code 1 indicates application startup failure, not OOM.
  The previous log snippet shows "FATAL: DATABASE_URL not set".
  The pod spec references a ConfigMap that may not exist.

SUGGESTED ACTIONS:
  [HIGH] 1. Verify the referenced ConfigMap exists
       $ kubectl get configmap app-config -n default
       $ kubectl describe configmap app-config -n default
       → Look for the DATABASE_URL key in the data section

  [HIGH] 2. Check pod environment variables
       $ kubectl describe pod nginx-abc123 -n default
       → Look for "Error" in the Environment section

  [MEDIUM] 3. View previous crash logs
       $ kubectl logs nginx-abc123 --previous -n default
       → Look for the exact startup error message

ADDITIONAL CHECKS:
  • Verify Secret objects referenced in the pod spec exist
  • Check if the ConfigMap was deleted recently with kubectl get events

Estimated resolution: quick (< 5 min)
======================================================================
```

---

## Development

```bash
# Clone and install (uses uv if available, venv fallback otherwise)
git clone https://github.com/msdeepak052/k8s-ai-support-kas.git
cd k8s-ai-support-kas
bash install.sh

# After any code change — editable install means changes are live immediately
# After a git pull — reinstall to pick up dependency changes
bash install.sh --update

# Run tests (unit tests, no cluster needed)
uv run pytest tests/ -v

# Run with kind cluster (integration tests)
kind create cluster --config tests/fixtures/kind_cluster.yaml
bash test-scenarios/apply-all.sh
uv run pytest tests/ -v -m integration

# Lint and format
uv run ruff check src/
uv run black src/ tests/
uv run mypy src/

# Build Docker image
docker build -t k8s-ai-support:dev .
```

---

## Docker

```bash
# Build
docker build -t k8s-ai-support:latest .

# Run single query
docker run --rm \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -v ~/.kube:/home/k8sai/.kube:ro \
  k8s-ai-support:latest \
  "why is my nginx pod crashing?"

# Run MCP server
docker run --rm -i \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -v ~/.kube:/home/k8sai/.kube:ro \
  k8s-ai-support:latest mcp
```

---

## Helm Deployment

```bash
# Deploy to Kubernetes
helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --create-namespace \
  --set apiKeys.openaiApiKey=$OPENAI_API_KEY \
  --set llm.provider=openai \
  --set llm.model=gpt-4o-mini

# Or use existing secret
kubectl create secret generic k8s-ai-secrets \
  --from-literal=OPENAI_API_KEY=$OPENAI_API_KEY \
  -n k8s-ai

helm install k8s-ai-support helm/k8s-ai-support/ \
  --set apiKeys.existingSecret=k8s-ai-secrets
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | - | OpenAI API key |
| `GEMINI_API_KEY` | - | Google Gemini API key |
| `ANTHROPIC_API_KEY` | - | Anthropic Claude API key |
| `K8S_AI_PROVIDER` | `openai` | LLM provider |
| `K8S_AI_MODEL` | `gpt-4o-mini` | Model name |
| `K8S_AI_TOKEN_BUDGET` | `8000` | Max tokens per LLM call |
| `K8S_AI_LOG_LEVEL` | `WARNING` | Base log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`). Normal runs are silent — use `--verbose` for INFO, `--debug` for full trace. |
| `K8S_AI_KUBECTL_TIMEOUT` | `10` | kubectl timeout (seconds) |
| `K8S_AI_MCP_RATE_LIMIT` | `10` | MCP requests/minute |
| `K8S_AI_OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `KUBECONFIG` | `~/.kube/config` | Kubeconfig path |

---

## Troubleshooting

**Diagnosis is generic / not using actual logs:**
```bash
# Run with --debug to see what kubectl fetched and what the LLM received
kas "query" -n namespace --debug

# Check whether logs were fetched and how large they are
kas "query" -n namespace --debug 2>&1 | grep "log\["

# If resource name wasn't auto-detected, pass it explicitly with -r
kas "query" -n namespace -r my-pod-name --debug
```

**`kas` command not found after install:**
```bash
# Check if ~/.local/bin is in PATH
echo $PATH | grep -q "$HOME/.local/bin" || echo "Add to ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\""
source ~/.bashrc   # then reload
kas version
```

**Changes in a new clone not reflected:**
```bash
# The old kas still points to the previous clone directory.
# Run install from inside the new clone to update the pointer.
cd /path/to/new/clone
bash install.sh --update
```

**Python 3.14 errors (packages fail to build):**
```bash
# Python 3.14 is not supported — use 3.12 explicitly
uv tool uninstall k8s-ai-support
uv tool install --python 3.12 --editable ".[all]"
```

**No cluster access:**
```bash
kas check
# If cluster unreachable, agent falls back to RAG-only mode using K8s documentation
```

**Missing API key:**
```bash
env | grep -E "(OPENAI|GEMINI|ANTHROPIC)_API_KEY"
# Agent auto-detects whichever key is set
```

**Slow first run:**
The first run downloads the embedding model (~130 MB for BAAI/bge-small-en-v1.5).
Subsequent runs use the cached model from `~/.cache/k8s-ai/`.

**High token usage:**
```bash
export K8S_AI_TOKEN_BUDGET=4000  # Reduce budget
export K8S_AI_MAX_LOG_LINES=5    # Fewer log lines
export K8S_AI_RAG_TOP_K=1        # Fewer RAG chunks
```

---