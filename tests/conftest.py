"""
Pytest configuration and shared fixtures for k8s-ai-support tests.
"""

import asyncio
import os
import sys

import pytest

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ─────────────────────────── Event Loop ──────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    """Create a single event loop for the test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ─────────────────────────── Settings Fixture ────────────────────────────────

@pytest.fixture(autouse=True)
def reset_settings():
    """Reset settings cache before each test to prevent state leakage."""
    from src.config.settings import reset_settings_cache
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def test_settings():
    """Test settings with a fake API key."""
    import os
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("OPENAI_API_KEY", "sk-test-key-for-unit-tests-only")
        mp.setenv("K8S_AI_PROVIDER", "openai")
        mp.setenv("K8S_AI_MODEL", "gpt-4o-mini")
        mp.setenv("K8S_AI_LOG_LEVEL", "DEBUG")
        from src.config.settings import get_settings, reset_settings_cache
        reset_settings_cache()
        settings = get_settings()
        yield settings


# ─────────────────────────── Common K8s Fixtures ─────────────────────────────

@pytest.fixture
def namespace():
    return "default"


@pytest.fixture
def pod_name():
    return "test-pod-abc123"


@pytest.fixture
def deployment_name():
    return "test-deployment"


@pytest.fixture
def sample_deployment_json():
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "test-deployment",
            "namespace": "default",
            "creationTimestamp": "2024-01-15T10:00:00Z",
        },
        "spec": {
            "replicas": 3,
            "selector": {"matchLabels": {"app": "test"}},
            "strategy": {"type": "RollingUpdate"},
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "myapp:v1.2.3",
                        }
                    ]
                }
            },
        },
        "status": {
            "replicas": 3,
            "readyReplicas": 1,
            "availableReplicas": 1,
            "updatedReplicas": 2,
            "conditions": [
                {
                    "type": "Available",
                    "status": "False",
                    "reason": "MinimumReplicasUnavailable",
                    "message": "Deployment does not have minimum availability.",
                }
            ],
        },
    }


@pytest.fixture
def sample_node_json():
    return {
        "apiVersion": "v1",
        "kind": "Node",
        "metadata": {
            "name": "worker-node-1",
            "labels": {
                "node-role.kubernetes.io/worker": "",
                "topology.kubernetes.io/zone": "us-east-1a",
            },
        },
        "spec": {
            "taints": [],
        },
        "status": {
            "conditions": [
                {"type": "Ready", "status": "True", "reason": "KubeletReady"},
                {"type": "MemoryPressure", "status": "False"},
                {"type": "DiskPressure", "status": "False"},
                {"type": "PIDPressure", "status": "False"},
            ],
            "capacity": {"cpu": "4", "memory": "8Gi"},
            "allocatable": {"cpu": "3800m", "memory": "7Gi"},
            "nodeInfo": {
                "kubeletVersion": "v1.35.0",
                "operatingSystem": "linux",
                "kernelVersion": "5.15.0-1056-aws",
                "containerRuntimeVersion": "containerd://1.7.13",
            },
        },
    }
