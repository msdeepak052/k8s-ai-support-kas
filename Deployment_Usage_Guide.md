# k8s-ai-support — Deployment & Usage Guide

All the ways to run `k8s-ai-support`, from a quick local test to a full production EKS deployment.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Local — CLI against any cluster](#2-local--cli-against-any-cluster)
3. [Local — MCP Server for IDE](#3-local--mcp-server-for-ide)
4. [Docker — single container](#4-docker--single-container)
5. [Kind — local Kubernetes testing](#5-kind--local-kubernetes-testing)
6. [EKS — CLI/MCP from local machine](#6-eks--climcp-from-local-machine)
7. [EKS — Helm deploy inside cluster](#7-eks--helm-deploy-inside-cluster)
8. [EKS — ECR + Helm (full AWS-native)](#8-eks--ecr--helm-full-aws-native)
9. [GKE — Google Kubernetes Engine](#9-gke--google-kubernetes-engine)
10. [AKS — Azure Kubernetes Service](#10-aks--azure-kubernetes-service)
11. [Air-gapped / Ollama (no internet LLM)](#11-air-gapped--ollama-no-internet-llm)
12. [Environment Variable Reference](#12-environment-variable-reference)

---

## 1. Prerequisites

### Required on your machine

```bash
# Python 3.11+
python --version

# uv (recommended package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# kubectl
curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
chmod +x kubectl && sudo mv kubectl /usr/local/bin/

# Verify
kubectl version --client
```

### Clone and install globally

```bash
git clone https://github.com/msdeepak052/k8s-ai-support-kas.git
cd k8s-ai-support

# One-time global install — registers kas and k8s-ai-support as system commands
uv tool install --editable ".[all]"
```

After this you never need `uv run` again. Both commands work from anywhere:

```bash
kas "why is my pod crashing?"          # short alias
k8s-ai-support "why is my pod crashing?"  # full name — same thing
```

To update after pulling new code:

```bash
uv tool upgrade k8s-ai-support
```

### API Key (at least one)

```bash
# OpenAI — https://platform.openai.com/api-keys
export OPENAI_API_KEY=sk-...

# OR Google Gemini — https://aistudio.google.com/app/apikey
export GEMINI_API_KEY=AIza...

# OR Anthropic Claude — https://console.anthropic.com/
export ANTHROPIC_API_KEY=sk-ant-...
```

---

## 2. Local — CLI against any cluster

Runs entirely on your machine. No deployment needed. Works with any cluster your `kubectl` can reach.

```bash
# Single query
kas "why is my nginx pod crashing?"

# Target a specific namespace
kas "pod failing" -n production

# Target a specific resource
kas "deployment not scaling" \
  -n production \
  -r nginx-deployment \
  -t deployment

# JSON output (good for scripting)
kas "crashloopbackoff in staging" -o json

# YAML output
kas "node not ready" -o yaml

# Verbose — shows token count, steps, execution time
kas "pvc pending" -v

# Interactive REPL mode
kas --interactive

# Check cluster connectivity and API key
kas check

# Switch provider on the fly
kas "pod crashing" \
  --provider gemini \
  --model gemini-1.5-flash
```

### Using .env file

```bash
cp .env.example .env
# Edit .env with your API keys and preferences
nano .env

# Agent auto-loads .env
kas "why is my pod pending?"
```

---

## 3. Local — MCP Server for IDE

Runs the agent as a background stdio server that your IDE connects to as a tool.

### Start the server manually (for testing)

```bash
kas mcp

# Test it responds correctly
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"clientInfo":{"name":"test"},"protocolVersion":"2024-11-05"}}' \
  | kas mcp
```

### VS Code — `settings.json`

```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "uv",
      "args": ["run", "k8s-ai-support", "mcp"],
      "cwd": "/path/to/k8s-ai-support",
      "env": {
        "OPENAI_API_KEY": "${env:OPENAI_API_KEY}",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini"
      }
    }
  }
}
```

### Cursor — `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "k8s-ai": {
      "command": "uv",
      "args": ["run", "k8s-ai-support", "mcp"],
      "cwd": "/path/to/k8s-ai-support",
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "K8S_AI_PROVIDER": "openai",
        "K8S_AI_MODEL": "gpt-4o-mini"
      }
    }
  }
}
```

### Claude Desktop — `claude_desktop_config.json`

macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "k8s-ai": {
      "command": "uv",
      "args": ["run", "k8s-ai-support", "mcp"],
      "cwd": "/path/to/k8s-ai-support",
      "env": {
        "OPENAI_API_KEY": "sk-...",
        "K8S_AI_PROVIDER": "openai"
      }
    }
  }
}
```

### Available MCP Tools in your IDE

| Tool | What it does |
|------|-------------|
| `k8s_diagnose` | Full AI diagnosis of any K8s issue |
| `k8s_get_resources` | List pods, deployments, nodes, etc. |
| `k8s_get_logs` | Fetch pod logs including crash logs |
| `k8s_describe` | Describe any resource |
| `k8s_get_events` | Get cluster warning events |

---

## 4. Docker — single container

No Python or uv needed. Just Docker and your kubeconfig.

### Build the image

```bash
docker build -t k8s-ai-support:latest .
```

### Run a single query

```bash
docker run --rm \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e K8S_AI_PROVIDER=openai \
  -e K8S_AI_MODEL=gpt-4o-mini \
  -v ~/.kube:/home/k8sai/.kube:ro \
  k8s-ai-support:latest \
  diagnose "why is my nginx pod crashing?" -n production
```

### Run MCP server (IDE integration via Docker)

```bash
docker run --rm -i \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -v ~/.kube:/home/k8sai/.kube:ro \
  k8s-ai-support:latest mcp
```

VS Code `settings.json` with Docker:

```json
{
  "mcp.servers": {
    "k8s-ai": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "OPENAI_API_KEY",
        "-v", "${env:HOME}/.kube:/home/k8sai/.kube:ro",
        "k8s-ai-support:latest", "mcp"
      ]
    }
  }
}
```

### Docker Compose

```bash
# Run agent (one-shot via docker compose run)
docker compose run --rm k8s-ai-support diagnose "pod crashing" -n default

# Run MCP server
docker compose --profile mcp up k8s-ai-mcp

# Run with local Ollama
docker compose --profile ollama up ollama
docker exec ollama ollama pull llama3.1
docker compose --profile agent up k8s-ai-support
```

---

## 5. Kind — local Kubernetes testing

Spin up a local cluster with broken pod scenarios for development and testing.

### Install Kind

```bash
# macOS
brew install kind

# Linux
curl -Lo ./kind https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64
chmod +x ./kind && sudo mv ./kind /usr/local/bin/
```

### Create cluster with test fixtures

```bash
# Create cluster (control-plane + 2 workers)
kind create cluster --config tests/fixtures/kind_cluster.yaml --wait 120s

# Point kubectl at it
kubectl config use-context kind-k8s-ai-test

# Apply broken pod scenarios
kubectl apply -f tests/fixtures/kind_cluster.yaml

# Wait for pods to start (and fail)
kubectl get pods -n k8s-ai-test -w
```

### Run diagnosis against kind cluster

```bash
# Diagnose the CrashLoopBackOff pod
kas "why is crashloop-pod failing?" \
  -n k8s-ai-test -r crashloop-pod -t pod

# Diagnose the pending pod
kas "why is pending-pod stuck?" \
  -n k8s-ai-test -r pending-pod -t pod

# Diagnose ImagePullBackOff
kas "imagepull-pod failing to start" \
  -n k8s-ai-test -r imagepull-pod

# Run all tests against the kind cluster
uv run pytest tests/ -v
```

### Tear down

```bash
kind delete cluster --name k8s-ai-test
```

---

## 6. EKS — CLI/MCP from local machine

Simplest EKS usage — run locally, kubectl talks to EKS. No cluster deployment needed.

### Configure kubeconfig

```bash
# Install AWS CLI
pip install awscli   # or: brew install awscli

# Authenticate
aws configure
# AWS Access Key ID: ...
# AWS Secret Access Key: ...
# Default region: ap-south-1

# Add EKS cluster to kubeconfig
aws eks update-kubeconfig \
  --region ap-south-1 \
  --name k8s-ai-support-cluster

# Verify
kubectl get nodes
kubectl get pods -A
```

### Run the agent

```bash
export OPENAI_API_KEY=sk-...

# Query your EKS cluster
kas "why is my backend pod crashing?" -n production

# Interactive session against EKS
kas --interactive
```

### MCP server pointed at EKS

The MCP server automatically uses your active kubeconfig, so once you run `aws eks update-kubeconfig` your IDE tools will query EKS.

```bash
# Confirm active context
kubectl config current-context
# Should show: arn:aws:eks:ap-south-1:339712902352:cluster/k8s-ai-support-cluster

# Start MCP server — it uses that context automatically
kas mcp
```

---

## 7. EKS — Helm deploy inside cluster

Deploys the agent as a pod inside your EKS cluster. Useful for team access or CI/CD pipelines.

### Step 1 — Enable IRSA on your cluster

```bash
eksctl utils associate-iam-oidc-provider \
  --region ap-south-1 \
  --cluster k8s-ai-support-cluster \
  --approve
```

### Step 2 — Create IAM policy (read-only)

```bash
cat > k8s-ai-readonly-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "eks:DescribeCluster",
        "eks:ListClusters"
      ],
      "Resource": "*"
    }
  ]
}
EOF

aws iam create-policy \
  --policy-name k8s-ai-support-readonly \
  --policy-document file://k8s-ai-readonly-policy.json
```

### Step 3 — Create IAM service account

```bash
eksctl create iamserviceaccount \
  --name k8s-ai-support \
  --namespace k8s-ai \
  --cluster k8s-ai-support-cluster \
  --region ap-south-1 \
  --attach-policy-arn arn:aws:iam::339712902352:policy/k8s-ai-support-readonly \
  --approve \
  --override-existing-serviceaccounts
```

### Step 4 — Store API key as Kubernetes Secret

```bash
kubectl create namespace k8s-ai

kubectl create secret generic k8s-ai-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  --namespace k8s-ai
```

### Step 5 — Deploy with Helm

```bash
helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --create-namespace \
  --set apiKeys.existingSecret=k8s-ai-secrets \
  --set llm.provider=openai \
  --set llm.model=gpt-4o-mini \
  --set serviceAccount.annotations."eks\.amazonaws\.com/role-arn"=\
arn:aws:iam::339712902352:role/eksctl-k8s-ai-support-role

# Verify deployment
helm status k8s-ai-support -n k8s-ai
kubectl get pods -n k8s-ai
kubectl logs -n k8s-ai deploy/k8s-ai-support
```

### Step 6 — Run a diagnosis from inside EKS

```bash
# One-shot query via kubectl exec
kubectl run k8s-ai-query --rm -it --restart=Never \
  --image=k8s-ai-support:latest \
  --env="OPENAI_API_KEY=sk-..." \
  --namespace k8s-ai \
  -- diagnose "why is my pod crashing?" -n production
```

### Upgrade / Uninstall

```bash
# Upgrade
helm upgrade k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --reuse-values \
  --set image.tag=1.1.0

# Uninstall
helm uninstall k8s-ai-support --namespace k8s-ai
kubectl delete namespace k8s-ai
```

---

## 8. EKS — ECR + Helm (full AWS-native)

Build the image, push to ECR, deploy via Helm. Best for production — no public Docker Hub dependency.

### Step 1 — Create ECR repository

```bash
aws ecr create-repository \
  --repository-name k8s-ai-support \
  --region ap-south-1 \
  --image-scanning-configuration scanOnPush=true

# Note the repositoryUri from output:
# 339712902352.dkr.ecr.ap-south-1.amazonaws.com/k8s-ai-support
```

### Step 2 — Build and push image

```bash
# Login to ECR
aws ecr get-login-password --region ap-south-1 | \
  docker login --username AWS \
  --password-stdin 339712902352.dkr.ecr.ap-south-1.amazonaws.com

# Build
docker build -t k8s-ai-support:1.0.0 .

# Tag
docker tag k8s-ai-support:1.0.0 \
  339712902352.dkr.ecr.ap-south-1.amazonaws.com/k8s-ai-support:1.0.0

# Push
docker push \
  339712902352.dkr.ecr.ap-south-1.amazonaws.com/k8s-ai-support:1.0.0
```

### Step 3 — Deploy from ECR

```bash
helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --create-namespace \
  --set image.repository=339712902352.dkr.ecr.ap-south-1.amazonaws.com/k8s-ai-support \
  --set image.tag=1.0.0 \
  --set image.pullPolicy=Always \
  --set apiKeys.existingSecret=k8s-ai-secrets \
  --set llm.provider=openai \
  --set llm.model=gpt-4o-mini

# Verify
kubectl get pods -n k8s-ai
kubectl describe pod -n k8s-ai -l app.kubernetes.io/name=k8s-ai-support
```

### Step 4 — CI/CD pipeline example (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
name: Build and Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: ap-south-1

      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push
        run: |
          docker build -t ${{ secrets.ECR_REPO }}:${{ github.sha }} .
          docker push ${{ secrets.ECR_REPO }}:${{ github.sha }}

      - name: Deploy to EKS
        run: |
          aws eks update-kubeconfig --region ap-south-1 --name k8s-ai-support-cluster
          helm upgrade --install k8s-ai-support helm/k8s-ai-support/ \
            --namespace k8s-ai \
            --set image.repository=${{ secrets.ECR_REPO }} \
            --set image.tag=${{ github.sha }} \
            --set apiKeys.existingSecret=k8s-ai-secrets
```

---

## 9. GKE — Google Kubernetes Engine

### Configure kubeconfig

```bash
# Install gcloud CLI — https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# Get kubeconfig for your GKE cluster
gcloud container clusters get-credentials your-cluster-name \
  --region us-central1 \
  --project YOUR_PROJECT_ID

# Verify
kubectl get nodes
```

### Deploy with Helm

```bash
kubectl create namespace k8s-ai

kubectl create secret generic k8s-ai-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  --namespace k8s-ai

helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --set apiKeys.existingSecret=k8s-ai-secrets \
  --set llm.provider=gemini \
  --set llm.model=gemini-1.5-flash
```

### Use Workload Identity (GKE equivalent of IRSA)

```bash
# Create GCP service account
gcloud iam service-accounts create k8s-ai-support \
  --display-name="k8s-ai-support"

# Bind to K8s service account via Workload Identity
gcloud iam service-accounts add-iam-policy-binding \
  k8s-ai-support@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --role roles/iam.workloadIdentityUser \
  --member "serviceAccount:YOUR_PROJECT_ID.svc.id.goog[k8s-ai/k8s-ai-support]"

# Annotate K8s service account
kubectl annotate serviceaccount k8s-ai-support \
  --namespace k8s-ai \
  iam.gke.io/gcp-service-account=k8s-ai-support@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

---

## 10. AKS — Azure Kubernetes Service

### Configure kubeconfig

```bash
# Install Azure CLI — https://learn.microsoft.com/en-us/cli/azure/install-azure-cli
az login
az account set --subscription YOUR_SUBSCRIPTION_ID

# Get kubeconfig
az aks get-credentials \
  --resource-group your-resource-group \
  --name your-cluster-name

# Verify
kubectl get nodes
```

### Deploy with Helm

```bash
kubectl create namespace k8s-ai

kubectl create secret generic k8s-ai-secrets \
  --from-literal=OPENAI_API_KEY=sk-... \
  --namespace k8s-ai

helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --set apiKeys.existingSecret=k8s-ai-secrets \
  --set llm.provider=openai \
  --set llm.model=gpt-4o-mini
```

### Use Azure Workload Identity (AKS equivalent of IRSA)

```bash
# Enable workload identity on cluster
az aks update \
  --resource-group your-resource-group \
  --name your-cluster-name \
  --enable-workload-identity \
  --enable-oidc-issuer

# Create managed identity
az identity create \
  --name k8s-ai-support \
  --resource-group your-resource-group

# Annotate K8s service account
kubectl annotate serviceaccount k8s-ai-support \
  --namespace k8s-ai \
  azure.workload.identity/client-id=YOUR_MANAGED_IDENTITY_CLIENT_ID
```

---

## 11. Air-gapped / Ollama (no internet LLM)

For environments with no internet access to OpenAI/Gemini/Claude. LLM runs fully locally via Ollama.

### Install Ollama

```bash
# Linux / macOS
curl -fsSL https://ollama.com/install.sh | sh

# Windows
# Download from https://ollama.com/download/windows

# Pull a model (do this before going air-gapped)
ollama pull llama3.1        # Good balance of speed/quality (8B)
ollama pull qwen2.5         # Alternative — strong at code/infra tasks
ollama pull mistral         # Lightweight option

# Verify Ollama is running
ollama list
```

### Run with Ollama

```bash
export K8S_AI_PROVIDER=ollama
export K8S_AI_MODEL=llama3.1
export K8S_AI_OLLAMA_URL=http://localhost:11434

kas "why is my pod crashing?" -n production
```

### Ollama via Docker Compose

```bash
# Start Ollama + agent together
docker compose --profile ollama up -d ollama

# Pull model into Ollama container
docker exec ollama ollama pull llama3.1

# Run agent pointing at Ollama
K8S_AI_PROVIDER=ollama K8S_AI_MODEL=llama3.1 \
  docker compose --profile agent up k8s-ai-support
```

### Deploy Ollama inside EKS (fully air-gapped)

```bash
# Deploy Ollama as a pod (GPU node recommended)
kubectl apply -f - <<EOF
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ollama
  namespace: k8s-ai
spec:
  replicas: 1
  selector:
    matchLabels:
      app: ollama
  template:
    metadata:
      labels:
        app: ollama
    spec:
      containers:
        - name: ollama
          image: ollama/ollama:latest
          ports:
            - containerPort: 11434
          resources:
            requests:
              memory: "8Gi"
              cpu: "2"
            limits:
              memory: "16Gi"
              cpu: "4"
---
apiVersion: v1
kind: Service
metadata:
  name: ollama
  namespace: k8s-ai
spec:
  selector:
    app: ollama
  ports:
    - port: 11434
      targetPort: 11434
EOF

# Pull model into the Ollama pod
kubectl exec -n k8s-ai deploy/ollama -- ollama pull llama3.1

# Deploy k8s-ai-support pointing at in-cluster Ollama
helm install k8s-ai-support helm/k8s-ai-support/ \
  --namespace k8s-ai \
  --set llm.provider=ollama \
  --set llm.model=llama3.1 \
  --set llm.ollamaUrl=http://ollama.k8s-ai.svc.cluster.local:11434
```

---

## 12. Environment Variable Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | OpenAI API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `K8S_AI_PROVIDER` | `openai` | LLM provider: `openai` \| `gemini` \| `claude` \| `ollama` |
| `K8S_AI_MODEL` | `gpt-4o-mini` | Model name for the selected provider |
| `K8S_AI_OLLAMA_URL` | `http://localhost:11434` | Ollama server URL |
| `K8S_AI_TOKEN_BUDGET` | `8000` | Max tokens sent to LLM per request |
| `K8S_AI_LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `K8S_AI_KUBECTL_TIMEOUT` | `10` | kubectl command timeout in seconds |
| `K8S_AI_DEFAULT_NAMESPACE` | `default` | Default namespace when none specified |
| `K8S_AI_MCP_RATE_LIMIT` | `10` | MCP server requests per minute |
| `K8S_AI_MAX_LOG_LINES` | `10` | Max container log lines included per query |
| `K8S_AI_RAG_TOP_K` | `3` | Number of K8s doc chunks retrieved per query |
| `K8S_AI_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | Local HuggingFace embedding model |
| `KUBECONFIG` | `~/.kube/config` | Path to kubeconfig file |

### Quick provider switch

```bash
# OpenAI
K8S_AI_PROVIDER=openai K8S_AI_MODEL=gpt-4o-mini kas "..."

# Gemini (free tier available)
K8S_AI_PROVIDER=gemini K8S_AI_MODEL=gemini-1.5-flash kas "..."

# Claude
K8S_AI_PROVIDER=claude K8S_AI_MODEL=claude-3-5-haiku-20241022 kas "..."

# Ollama (local, free)
K8S_AI_PROVIDER=ollama K8S_AI_MODEL=llama3.1 kas "..."
```

---

## Choosing the right deployment

| Your situation | Recommended option |
|---|---|
| Just trying it out | [Option 2](#2-local--cli-against-any-cluster) — local CLI |
| Using VS Code / Cursor / Claude Desktop | [Option 3](#3-local--mcp-server-for-ide) — local MCP server |
| No Python, just Docker | [Option 4](#4-docker--single-container) — Docker |
| Local K8s testing / development | [Option 5](#5-kind--local-kubernetes-testing) — Kind |
| EKS, quick start | [Option 6](#6-eks--climcp-from-local-machine) — local CLI against EKS |
| EKS, team shared access | [Option 7](#7-eks--helm-deploy-inside-cluster) — Helm in EKS |
| EKS, production / CI-CD | [Option 8](#8-eks--ecr--helm-full-aws-native) — ECR + Helm |
| Google Cloud (GKE) | [Option 9](#9-gke--google-kubernetes-engine) — GKE |
| Azure (AKS) | [Option 10](#10-aks--azure-kubernetes-service) — AKS |
| Air-gapped / no internet | [Option 11](#11-air-gapped--ollama-no-internet-llm) — Ollama |
