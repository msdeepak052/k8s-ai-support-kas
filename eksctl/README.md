# EKS Cluster Setup — k8s-ai-support-cluster

Provisions the `k8s-ai-support-cluster` EKS cluster in `ap-south-1` using eksctl.
Cluster sizing: **t3.xlarge (4 vCPU / 16 GB) × 2 nodes = 8 vCPU / 32 GB** — gives ~3× headroom for running `kas`.

---

## Install Required Tools

### 1. AWS CLI

```bash
# Download and install
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install

# Verify (should be 2.x)
aws --version

# Configure credentials
aws configure

# Confirm you are logged in
aws sts get-caller-identity
```

### 2. eksctl

```bash
# For ARM systems, set ARCH to: arm64, armv6, or armv7
ARCH=amd64
PLATFORM=$(uname -s)_$ARCH

curl -sLO "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_${PLATFORM}.tar.gz"

# (Optional) Verify checksum
curl -sL "https://github.com/eksctl-io/eksctl/releases/latest/download/eksctl_checksums.txt" \
  | grep $PLATFORM | sha256sum --check

tar -xzf eksctl_${PLATFORM}.tar.gz -C /tmp && rm eksctl_${PLATFORM}.tar.gz
sudo install -m 0755 /tmp/eksctl /usr/local/bin && rm /tmp/eksctl

# Verify
eksctl version
```

### 3. kubectl

```bash
# Download latest stable release
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"

# Install (requires root)
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl

# No root? Install to user bin instead
chmod +x kubectl
mkdir -p ~/.local/bin
mv ./kubectl ~/.local/bin/kubectl
# Make sure ~/.local/bin is in your $PATH

# Verify
kubectl version --client
```

---

## Create the Cluster

### Option A — eksctl Config File (Recommended)

The config file is already saved in this folder as `eks-k8s-ai-support-cluster.yaml`.

```bash
# From the repo root
eksctl create cluster -f eksctl/eks-k8s-ai-support-cluster.yaml
```

Takes **15–20 minutes**. eksctl handles VPC, subnets, IAM roles, node groups, and kubeconfig automatically.

### Option B — One-liner

```bash
eksctl create cluster \
  --name k8s-ai-support-cluster \
  --region ap-south-1 \
  --version 1.34 \
  --nodegroup-name k8s-ai-nodes \
  --node-type t3.xlarge \
  --nodes 2 \
  --nodes-min 2 \
  --nodes-max 3 \
  --node-volume-size 50 \
  --managed \
  --with-oidc \
  --endpoint-public-access \
  --no-endpoint-private-access \
  --tags "Environment=testing,Project=k8stools"
```

---

## Post-Create Steps

```bash
# Point kubectl at the new cluster
aws eks update-kubeconfig --name k8s-ai-support-cluster --region ap-south-1

# Make gp2 the default StorageClass (needed for PVC scenarios)
kubectl patch storageclass gp2 \
  -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'
```

---

## Verify the Cluster

```bash
# Confirm context is pointing at EKS
kubectl config current-context
# Expected: arn:aws:eks:ap-south-1:<account-id>:cluster/k8s-ai-support-cluster

# Check nodes are Ready
kubectl get nodes -o wide
```

Expected output:

```
NAME                                            STATUS   ROLES    AGE     VERSION
ip-192-168-49-121.ap-south-1.compute.internal   Ready    <none>   6m57s   v1.34.4-eks-f69f56f
ip-192-168-68-190.ap-south-1.compute.internal   Ready    <none>   6m58s   v1.34.4-eks-f69f56f
```

---

## Run kas Against the Cluster

```bash
# Set your LLM key
export K8S_AI_OPENAI_API_KEY=sk-...        # OpenAI
# or
export K8S_AI_GOOGLE_API_KEY=AIza...       # Gemini
# or
export K8S_AI_ANTHROPIC_API_KEY=sk-ant-... # Claude

# Basic diagnosis
kas "why are pods crashing in default namespace?"

# Target a specific resource
kas "failing-deployment not ready" -n kas-test -r failing-deployment -t deployment

# Apply test scenarios against this cluster
kubectl apply -f test-scenarios/00-namespace.yaml
bash test-scenarios/apply-all.sh

# Then diagnose each failure
kas "why is crash-loop-pod crashing?" -n kas-test
kas "oom-pod keeps restarting" -n kas-test
kas "why is stuck-pvc not binding?" -n kas-test -r stuck-pvc -t pvc
```

---

## Delete the Cluster

```bash
eksctl delete cluster --name k8s-ai-support-cluster --region ap-south-1
```

> This removes the cluster, node groups, VPC, and all associated AWS resources. Takes ~10 minutes.
