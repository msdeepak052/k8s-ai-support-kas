# VS Code / Cursor / Kiro MCP Integration

This guide shows how to connect `k8s-ai-support` as an MCP server to your IDE.

---

## Prerequisites

- `k8s-ai-support` installed: `pip install k8s-ai-support[all]`
- At least one API key set in your environment
- `kubectl` configured with access to your cluster

---

## VS Code Setup

### Step 1: Install the MCP Extension

Install the **Continue** or **Copilot Chat** extension that supports MCP servers,
or use VS Code's built-in MCP support (requires VS Code 1.96+).

### Step 2: Configure MCP Server

Add to your VS Code `settings.json` (`Ctrl+Shift+P` → "Open User Settings (JSON)"):

```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "${env:OPENAI_API_KEY}",
        "GEMINI_API_KEY": "${env:GEMINI_API_KEY}",
        "ANTHROPIC_API_KEY": "${env:ANTHROPIC_API_KEY}",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini",
        "K8S_AI_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

### Step 3: Verify Connection

In VS Code, open the MCP panel (if available) or check the Output panel for MCP logs.
You should see `k8s-ai-support` connected.

### Step 4: Use in Chat

In VS Code Chat or Continue:
```
@k8s-ai diagnose why my nginx pod is in CrashLoopBackOff in namespace production
```

---

## Cursor Setup

Add to `~/.cursor/mcp.json` (or `.cursor/mcp.json` in your project):

```json
{
  "mcpServers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini"
      }
    }
  }
}
```

Restart Cursor. The MCP server will appear in the tools panel.

---

## Kiro (AWS) Setup

Add to your Kiro MCP configuration:

```json
{
  "mcp": {
    "servers": {
      "k8s-ai": {
        "command": "k8s-ai-support",
        "args": ["mcp"],
        "env": {
          "OPENAI_API_KEY": "${OPENAI_API_KEY}",
          "K8S_AI_PROVIDER": "openai"
        }
      }
    }
  }
}
```

---

## Claude Desktop Setup

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS)
or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini",
        "K8S_AI_LOG_LEVEL": "WARNING"
      }
    }
  }
}
```

---

## Available MCP Tools

Once connected, these tools are available in your IDE:

| Tool | Description |
|------|-------------|
| `k8s_diagnose` | AI diagnosis of Kubernetes issues |
| `k8s_get_resources` | List any Kubernetes resource |
| `k8s_get_logs` | Fetch pod logs |
| `k8s_describe` | Describe a resource |
| `k8s_get_events` | Get cluster events |

---

## Example IDE Interactions

**Diagnose a pod:**
```
Use k8s_diagnose to investigate why pod "nginx-abc123" in namespace "production" is failing
```

**Check cluster state:**
```
Use k8s_get_resources to list all pods in namespace "staging"
```

**Investigate logs:**
```
Use k8s_get_logs to fetch logs from pod "backend-xyz" including previous container crashes
```

---

## Troubleshooting MCP Connection

### Server not starting
```bash
# Test the MCP server directly
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}' | k8s-ai-support mcp
```

Expected output:
```json
{"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "k8s-ai-support", "version": "1.0.0"}, "capabilities": {"tools": {"listChanged": false}}}}
```

### API key not found
Ensure the environment variable is set before starting VS Code:
```bash
export OPENAI_API_KEY=sk-...
code .  # Start VS Code from the terminal with env vars set
```

### kubectl not found
```bash
which kubectl  # Should return a path
kubectl get nodes  # Should list nodes
```

### Rate limit errors
The MCP server limits to 10 requests/minute by default.
Increase with: `K8S_AI_MCP_RATE_LIMIT=30`

---

## Using Gemini (Free tier available)

```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "GEMINI_API_KEY": "AIza...",
        "K8S_AI_PROVIDER": "gemini",
        "K8S_AI_MODEL": "gemini-1.5-flash"
      }
    }
  }
}
```

## Using Local Ollama (Air-gapped / Free)

```bash
# First, start Ollama and pull a model
ollama pull llama3.1

# Configure MCP
```

```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "k8s-ai-support",
      "args": ["mcp"],
      "env": {
        "K8S_AI_PROVIDER": "ollama",
        "K8S_AI_MODEL": "llama3.1",
        "K8S_AI_OLLAMA_URL": "http://localhost:11434"
      }
    }
  }
}
```
