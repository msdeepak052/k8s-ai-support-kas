"""
RAG (Retrieval-Augmented Generation) over Kubernetes documentation.
Uses local HuggingFace embeddings — NO external API calls for embedding.
Caches in ~/.cache/k8s-ai/
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy imports — only loaded when RAG is actually used
_chroma_client = None
_collection = None
_embedder = None


def _get_cache_dir() -> Path:
    from ..config.settings import get_settings
    settings = get_settings()
    d = settings.cache_dir / "rag"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_embedder(model_name: str):
    """Lazily load HuggingFace sentence-transformers embedder."""
    global _embedder
    if _embedder is not None:
        return _embedder

    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s (device=cpu)", model_name)
        # Force CPU — avoids CUDA compatibility issues with older GPUs (CC < 7.5)
        _embedder = SentenceTransformer(model_name, device="cpu")
        logger.info("Embedding model loaded on CPU")
        return _embedder
    except ImportError as exc:
        logger.warning("sentence-transformers unavailable (%s). RAG will use keyword fallback.", exc)
        return None
    except Exception as exc:
        logger.warning("Failed to load embedding model (%s). RAG will use keyword fallback.", exc)
        return None


def _chunk_text(text: str, chunk_size: int = 512, overlap: int = 50) -> List[str]:
    """
    Split text into overlapping chunks of approximately chunk_size tokens.
    Uses character-based splitting (1 token ≈ 4 chars).
    """
    char_size = chunk_size * 4
    char_overlap = overlap * 4
    chunks = []
    start = 0
    while start < len(text):
        end = start + char_size
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk.strip())
        start += char_size - char_overlap
    return chunks


# ─────────────────────── Built-in K8s Knowledge Base ────────────────────────

K8S_KNOWLEDGE_BASE = [
    {
        "id": "crashloopbackoff",
        "title": "CrashLoopBackOff Troubleshooting",
        "content": """
CrashLoopBackOff occurs when a container repeatedly starts and crashes.
Kubernetes uses exponential backoff (10s, 20s, 40s, 80s, 160s, 300s max) before restarting.

Common causes:
1. Application bug: process exits non-zero. Check logs: kubectl logs <pod> --previous
2. Missing ConfigMap or Secret: container environment variable not found
3. Port already in use (hostPort conflict)
4. Insufficient memory: OOMKilled (exit code 137)
5. Wrong command/entrypoint in Dockerfile
6. Health probe failure: readinessProbe or livenessProbe misconfigured
7. Missing volume mount: file/directory doesn't exist in container
8. Image pull issue: wrong tag or private registry auth

Diagnostic commands:
  kubectl describe pod <pod-name> -n <namespace>
  kubectl logs <pod-name> --previous -n <namespace>
  kubectl get events --field-selector involvedObject.name=<pod-name> -n <namespace>
  kubectl get pod <pod-name> -o yaml | grep -A5 resources

Fix checklist:
- Check exit code in lastState.terminated.exitCode
- Review last 50 lines of --previous logs
- Verify all environment variables from ConfigMaps/Secrets exist
- Check resource requests/limits (OOMKilled = exit 137)
- Verify liveness probe thresholds are realistic for startup time
"""
    },
    {
        "id": "pending_pod",
        "title": "Pod Stuck in Pending State",
        "content": """
A Pod in Pending state means the scheduler cannot place it on any node.

Common causes:
1. Insufficient resources: No node has enough CPU/memory
   - Check: kubectl describe node | grep -A5 "Allocated resources"
2. Taint/toleration mismatch: Node is tainted, pod lacks toleration
   - Check: kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints
3. NodeSelector or affinity rules: No node matches pod's nodeSelector
   - Check: pod spec.nodeSelector vs node labels
4. PersistentVolumeClaim not bound: Pod waits for PVC
   - Check: kubectl get pvc -n <namespace>
5. Unschedulable nodes: All nodes are cordoned/NotReady
6. Image pull failure before scheduling (rare)

Diagnostic commands:
  kubectl describe pod <pod-name> -n <namespace>  # Look for Events at bottom
  kubectl get nodes --show-labels
  kubectl get pvc -n <namespace>
  kubectl describe node <node-name>

Events to look for:
  - "0/3 nodes are available: 3 Insufficient cpu"
  - "didn't match node selector"
  - "had taint ... that the pod didn't tolerate"
"""
    },
    {
        "id": "imagepullbackoff",
        "title": "ImagePullBackOff and ErrImagePull",
        "content": """
ImagePullBackOff means Kubernetes cannot pull the container image.

Common causes:
1. Wrong image name or tag (typo, deleted tag)
2. Private registry: missing imagePullSecret
3. Rate limiting: Docker Hub pulls limited to 100/6h (anonymous)
4. Registry unreachable: network policy blocking egress
5. Image architecture mismatch (amd64 vs arm64)

Diagnostic commands:
  kubectl describe pod <pod-name> | grep -A10 Events
  kubectl get events --field-selector reason=Failed -n <namespace>

Fix:
  # Check image exists
  docker pull <image:tag>

  # Create registry secret
  kubectl create secret docker-registry regcred \
    --docker-server=<registry> \
    --docker-username=<user> \
    --docker-password=<pass>

  # Patch pod/deployment to use secret
  # spec.imagePullSecrets: [{name: regcred}]
"""
    },
    {
        "id": "oomkilled",
        "title": "OOMKilled - Out of Memory",
        "content": """
OOMKilled (exit code 137) means the container exceeded its memory limit.
The Linux kernel OOM killer terminated the process.

Symptoms:
- Exit code: 137
- lastState.terminated.reason: OOMKilled
- High memory usage before crash

Diagnostic commands:
  kubectl top pod <pod-name> -n <namespace>
  kubectl describe pod <pod-name> | grep -A5 "Last State"
  kubectl get pod <pod-name> -o jsonpath='{.spec.containers[*].resources}'

Solutions:
1. Increase memory limit:
   resources:
     limits:
       memory: "512Mi"  # Increase from current value
     requests:
       memory: "256Mi"

2. Fix memory leak in application code
3. Enable Vertical Pod Autoscaler (VPA)
4. Profile with: kubectl exec <pod> -- cat /proc/meminfo

Memory units: 128Mi, 256Mi, 512Mi, 1Gi, 2Gi
"""
    },
    {
        "id": "service_not_reachable",
        "title": "Service Not Reachable / Connection Refused",
        "content": """
Pod cannot connect to a Service or external endpoint.

Common causes:
1. Service selector doesn't match pod labels
2. Pod port mismatch: service.port.targetPort != container.ports.containerPort
3. NetworkPolicy blocking traffic
4. Service endpoints empty (no healthy pods)
5. DNS resolution failing (CoreDNS issue)

Diagnostic commands:
  # Check service endpoints
  kubectl get endpoints <service-name> -n <namespace>
  kubectl describe service <service-name> -n <namespace>

  # Check pod labels match service selector
  kubectl get pods --show-labels -n <namespace>
  kubectl get svc <service-name> -o yaml | grep selector -A5

  # Test DNS from within cluster
  kubectl run debug --image=busybox -it --rm -- nslookup <service-name>

  # Check NetworkPolicies
  kubectl get networkpolicies -n <namespace>
  kubectl describe networkpolicy <policy-name> -n <namespace>

Service endpoint empty = no pods match selector = label mismatch
"""
    },
    {
        "id": "pvc_pending",
        "title": "PersistentVolumeClaim Stuck in Pending",
        "content": """
PVC in Pending state means no PersistentVolume can satisfy the claim.

Common causes:
1. No StorageClass available or wrong storageClassName
2. No PV with matching access mode (ReadWriteOnce, ReadWriteMany, ReadOnlyMany)
3. Requested storage capacity exceeds available PV
4. StorageClass provisioner not installed (e.g., no CSI driver)
5. Topology constraints (zone affinity)

Diagnostic commands:
  kubectl describe pvc <pvc-name> -n <namespace>
  kubectl get storageclass
  kubectl get pv
  kubectl describe pv <pv-name>

Events to look for:
  - "no persistent volumes available for this claim"
  - "storageclass.storage.k8s.io not found"
  - "waiting for a volume to be created"

Fix:
  # Check available storage classes
  kubectl get storageclass
  # Ensure correct storageClassName in PVC spec
  # Or provision a PV manually if using static provisioning
"""
    },
    {
        "id": "node_not_ready",
        "title": "Node NotReady",
        "content": """
A Node in NotReady state means the kubelet is not reporting to the API server.

Common causes:
1. Node VM/instance crashed or stopped
2. Kubelet service stopped: systemctl status kubelet
3. Network partition: node isolated from control plane
4. Disk pressure: node disk is full
5. Memory pressure: node is OOM
6. PID pressure: too many processes

Diagnostic commands:
  kubectl describe node <node-name>  # Check Conditions section
  kubectl get events --field-selector involvedObject.name=<node-name>
  kubectl top nodes

Node conditions to check:
  - MemoryPressure: True = low memory
  - DiskPressure: True = low disk
  - PIDPressure: True = too many PIDs
  - Ready: False = kubelet not healthy

Pods on NotReady nodes get evicted after tolerationSeconds (default 300s).
"""
    },
    {
        "id": "deployment_rollout_stuck",
        "title": "Deployment Rollout Stuck / Deadlocked",
        "content": """
Deployment rollout is stuck when new ReplicaSet pods cannot become Ready.

Common causes:
1. New pods crash (same root causes as CrashLoopBackOff)
2. maxUnavailable=0 with minReadySeconds > 0 deadlock
3. PodDisruptionBudget blocking scale-down
4. Resource quota exceeded
5. New image fails health checks

Diagnostic commands:
  kubectl rollout status deployment/<name> -n <namespace>
  kubectl describe deployment <name> -n <namespace>
  kubectl get replicasets -n <namespace>
  kubectl describe replicaset <new-rs-name> -n <namespace>

To check rollout progress:
  kubectl get deployment <name> -o wide
  # READY vs DESIRED shows stuck rollout

RollingUpdate defaults:
  maxSurge: 25% (extra pods created)
  maxUnavailable: 25% (old pods removed)
"""
    },
    {
        "id": "hpa_not_scaling",
        "title": "HorizontalPodAutoscaler Not Scaling",
        "content": """
HPA not scaling when expected can have several causes.

Diagnostic commands:
  kubectl describe hpa <hpa-name> -n <namespace>
  kubectl get hpa -n <namespace>
  kubectl top pods -n <namespace>

Common causes:
1. Metrics server not installed: kubectl top returns error
2. Resource requests not set: HPA needs requests to calculate utilization
3. Metric value below threshold
4. Already at maxReplicas
5. Scale-down cooldown (stabilizationWindowSeconds, default 300s)
6. Custom metrics API not configured

HPA requires:
  - metrics-server running
  - Resource requests defined in pod spec
  - Target utilization set correctly (80% is common)
"""
    },
    {
        "id": "ingress_not_working",
        "title": "Ingress Not Working / 502 Bad Gateway",
        "content": """
Ingress not routing traffic correctly.

Common causes:
1. Ingress controller not installed (nginx, traefik, etc.)
2. Wrong ingressClassName
3. Backend service not found or wrong port
4. TLS secret missing or expired
5. Path routing mismatch
6. Upstream pod not ready (502 Bad Gateway)

Diagnostic commands:
  kubectl get ingress -n <namespace>
  kubectl describe ingress <ingress-name> -n <namespace>
  kubectl get pods -n ingress-nginx  # Check controller pods
  kubectl logs -n ingress-nginx <controller-pod> | tail -50

  # Check backend service
  kubectl get endpoints <backend-service> -n <namespace>

502 Bad Gateway from NGINX Ingress = upstream pod not ready or refusing connections.
Check: pod readiness probe, service endpoint, target port.
"""
    },
]


class RAGRetriever:
    """
    Local RAG over Kubernetes documentation.
    Embeddings are computed once and cached. No external API calls.
    """

    def __init__(self, settings=None):
        from ..config.settings import get_settings
        self.settings = settings or get_settings()
        self.cache_dir = _get_cache_dir()
        self._collection = None
        self._initialized = False

    def _get_embedder(self):
        return _load_embedder(self.settings.embedding_model)

    def _initialize_vector_store(self):
        """Initialize ChromaDB with embedded K8s docs."""
        if self._initialized:
            return

        embedder = self._get_embedder()
        if embedder is None:
            logger.warning("Embedder unavailable, RAG disabled")
            self._initialized = True
            return

        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            chroma_path = str(self.cache_dir / "chroma")
            client = chromadb.PersistentClient(
                path=chroma_path,
                settings=ChromaSettings(anonymized_telemetry=False),
            )

            collection_name = f"k8s_docs_{self.settings.k8s_docs_version}"

            # Check if collection already exists with docs
            try:
                collection = client.get_collection(collection_name)
                count = collection.count()
                if count > 0:
                    logger.info("RAG collection loaded: %d chunks", count)
                    self._collection = collection
                    self._initialized = True
                    return
            except Exception:
                pass

            # Create and populate collection
            logger.info("Initializing RAG knowledge base...")
            collection = client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine"},
            )

            all_documents = []
            all_embeddings = []
            all_metadatas = []
            all_ids = []

            for doc in K8S_KNOWLEDGE_BASE:
                chunks = _chunk_text(doc["content"], chunk_size=self.settings.rag_chunk_size)
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{doc['id']}_{i}"
                    embedding = embedder.encode(chunk).tolist()
                    all_documents.append(chunk)
                    all_embeddings.append(embedding)
                    all_metadatas.append({"title": doc["title"], "source": doc["id"]})
                    all_ids.append(chunk_id)

            if all_documents:
                collection.add(
                    documents=all_documents,
                    embeddings=all_embeddings,
                    metadatas=all_metadatas,
                    ids=all_ids,
                )
                logger.info("RAG initialized with %d chunks", len(all_documents))

            self._collection = collection
            self._initialized = True

        except ImportError:
            logger.warning("chromadb not installed. RAG disabled. Install with: pip install chromadb")
            self._initialized = True
        except Exception as exc:
            logger.error("Failed to initialize RAG: %s", exc)
            self._initialized = True

    def retrieve(self, query: str, top_k: Optional[int] = None) -> str:
        """
        Retrieve top-k relevant chunks for the query.
        Returns formatted string for inclusion in LLM context.
        """
        logger.debug("[RAG] retrieve called: query=%r", query)
        self._initialize_vector_store()

        if self._collection is None:
            logger.debug("[RAG] no vector collection — using keyword fallback")
            return self._fallback_retrieve(query)

        k = top_k or self.settings.rag_top_k
        embedder = self._get_embedder()
        if embedder is None:
            logger.debug("[RAG] embedder unavailable — using keyword fallback")
            return self._fallback_retrieve(query)

        logger.debug("[RAG] semantic search: top_k=%d", k)
        try:
            query_embedding = embedder.encode(query).tolist()
            results = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=k,
                include=["documents", "metadatas", "distances"],
            )

            if not results or not results["documents"]:
                logger.debug("[RAG] empty results from vector store — using keyword fallback")
                return self._fallback_retrieve(query)

            docs = results["documents"][0]
            metas = results["metadatas"][0]
            distances = results["distances"][0]

            # Format for LLM
            formatted_chunks = []
            for doc, meta, dist in zip(docs, metas, distances):
                relevance = 1.0 - dist  # cosine distance → similarity
                logger.debug("[RAG] chunk: title=%r relevance=%.3f", meta.get("title"), relevance)
                if relevance < 0.3:  # Skip low-relevance chunks
                    logger.debug("[RAG]   → skipped (below 0.3 threshold)")
                    continue
                formatted_chunks.append(
                    f"[K8s Docs: {meta.get('title', 'Unknown')} | Relevance: {relevance:.2f}]\n{doc}"
                )

            if not formatted_chunks:
                logger.debug("[RAG] no chunks above relevance threshold — using keyword fallback")
                return self._fallback_retrieve(query)

            logger.debug("[RAG] returning %d semantic chunks (%d total chars)",
                         len(formatted_chunks),
                         sum(len(c) for c in formatted_chunks))
            return "\n\n---\n\n".join(formatted_chunks)

        except Exception as exc:
            logger.warning("RAG retrieval failed: %s", exc)
            return self._fallback_retrieve(query)

    def _fallback_retrieve(self, query: str) -> str:
        """Keyword-based fallback when vector search unavailable."""
        logger.debug("[RAG-FALLBACK] keyword search for query=%r", query)
        query_lower = query.lower()
        matches = []

        keyword_map = {
            "crashloop": ["crashloopbackoff"],
            "crash": ["crashloopbackoff", "oomkilled"],
            "pending": ["pending_pod"],
            "imagepull": ["imagepullbackoff"],
            "image": ["imagepullbackoff"],
            "oom": ["oomkilled"],
            "memory": ["oomkilled"],
            "service": ["service_not_reachable"],
            "endpoint": ["service_not_reachable"],
            "pvc": ["pvc_pending"],
            "volume": ["pvc_pending"],
            "node": ["node_not_ready"],
            "notready": ["node_not_ready"],
            "rollout": ["deployment_rollout_stuck"],
            "deployment": ["deployment_rollout_stuck"],
            "hpa": ["hpa_not_scaling"],
            "autoscal": ["hpa_not_scaling"],
            "ingress": ["ingress_not_working"],
            "502": ["ingress_not_working"],
            "gateway": ["ingress_not_working"],
        }

        matched_ids = set()
        for keyword, doc_ids in keyword_map.items():
            if keyword in query_lower:
                matched_ids.update(doc_ids)

        if not matched_ids:
            # Return first 2 most general docs
            matched_ids = {"crashloopbackoff", "pending_pod"}

        logger.debug("[RAG-FALLBACK] matched doc IDs: %s", matched_ids)
        docs = [d for d in K8S_KNOWLEDGE_BASE if d["id"] in matched_ids]
        result = "\n\n---\n\n".join(
            f"[K8s Docs: {d['title']}]\n{d['content'][:800]}"
            for d in docs[:self.settings.rag_top_k]
        )
        logger.debug("[RAG-FALLBACK] returning %d docs (%d chars)", len(docs), len(result))
        return result
