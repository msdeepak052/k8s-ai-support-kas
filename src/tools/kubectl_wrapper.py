"""
Async, read-only kubectl wrapper with hard-enforced blocklist.
All cluster reads go through this module — mutations are impossible.
"""

import asyncio
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config.blocklist import is_command_safe
from ..config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class KubectlResult:
    """Structured result from a kubectl command."""

    success: bool
    command: str
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    parsed: Optional[Any] = None  # JSON-parsed if available
    error_message: Optional[str] = None

    @property
    def is_not_found(self) -> bool:
        return "not found" in (self.stderr + self.stdout).lower()

    @property
    def is_timeout(self) -> bool:
        return self.error_message == "timeout"


class KubectlWrapper:
    """
    Async kubectl wrapper — strictly read-only.
    Enforces blocklist before every execution.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        self._kubectl_path = shutil.which("kubectl")
        if not self._kubectl_path:
            logger.warning("kubectl not found in PATH. Cluster operations will fail.")

    def _base_args(self, namespace: Optional[str] = None, output_json: bool = True) -> list[str]:
        """Build common kubectl arguments."""
        args = []
        if self.settings.kubeconfig:
            args += ["--kubeconfig", self.settings.kubeconfig]
        if namespace and namespace != "all":
            args += ["--namespace", namespace]
        elif namespace == "all":
            args += ["--all-namespaces"]
        if output_json:
            args += ["--output", "json"]
        return args

    async def _run(
        self,
        subcommand: str,
        args: list[str],
        namespace: Optional[str] = None,
        output_json: bool = True,
        timeout: Optional[int] = None,
    ) -> KubectlResult:
        """
        Internal async executor for kubectl commands.
        Always validates against blocklist before running.
        """
        if not self._kubectl_path:
            return KubectlResult(
                success=False,
                command=subcommand,
                error_message="kubectl not found in PATH",
            )

        # Build full command string for validation
        full_cmd_str = f"kubectl {subcommand} {' '.join(args)}"
        is_safe, reason = is_command_safe(full_cmd_str)
        if not is_safe:
            logger.error("BLOCKED unsafe command: %s | Reason: %s", full_cmd_str, reason)
            return KubectlResult(
                success=False,
                command=full_cmd_str,
                error_message=f"BLOCKED: {reason}",
            )

        # Build actual command list
        cmd = [self._kubectl_path, subcommand]
        cmd.extend(self._base_args(namespace=namespace, output_json=output_json))
        cmd.extend(args)

        timeout_secs = timeout or self.settings.kubectl_timeout

        logger.debug("[KUBECTL] exec: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_secs
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("kubectl command timed out after %ds: %s", timeout_secs, " ".join(cmd))
                return KubectlResult(
                    success=False,
                    command=" ".join(cmd),
                    error_message="timeout",
                )

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            rc = proc.returncode

            # Attempt JSON parse
            parsed = None
            if output_json and stdout and rc == 0:
                try:
                    parsed = json.loads(stdout)
                except json.JSONDecodeError:
                    pass

            result = KubectlResult(
                success=(rc == 0),
                command=" ".join(cmd),
                stdout=stdout,
                stderr=stderr,
                return_code=rc,
                parsed=parsed,
            )

            if rc != 0:
                logger.warning("[KUBECTL] rc=%d | cmd: %s", rc, " ".join(cmd))
                logger.debug("[KUBECTL] stderr: %s", stderr[:300] if stderr else "(empty)")
            else:
                logger.debug("[KUBECTL] OK rc=0 | stdout=%d chars | cmd: %s",
                             len(stdout), " ".join(cmd))

            return result

        except Exception as exc:
            logger.exception("Unexpected error running kubectl: %s", exc)
            return KubectlResult(
                success=False,
                command=" ".join(cmd),
                error_message=str(exc),
            )

    # -------------------------------------------------------------------------
    # Public read-only API
    # -------------------------------------------------------------------------

    async def get_pods(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["pods"], namespace=namespace)

    async def get_resource(self, resource_type: str, name: Optional[str] = None, namespace: str = "default") -> KubectlResult:
        """Get any Kubernetes resource."""
        args = [resource_type]
        if name:
            args.append(name)
        return await self._run("get", args, namespace=namespace)

    async def get_all_namespaces(self, resource_type: str) -> KubectlResult:
        """Get resource across all namespaces."""
        return await self._run("get", [resource_type], namespace="all")

    async def describe_resource(self, resource_type: str, name: str, namespace: str = "default") -> KubectlResult:
        """Describe a specific resource (plain text output)."""
        return await self._run(
            "describe",
            [resource_type, name],
            namespace=namespace,
            output_json=False,
        )

    async def get_logs(
        self,
        pod_name: str,
        namespace: str = "default",
        container: Optional[str] = None,
        previous: bool = False,
        tail: int = 100,
    ) -> KubectlResult:
        """Fetch pod logs — last N lines, optionally previous container."""
        logger.debug("[KUBECTL-LOGS] pod=%s, ns=%s, previous=%s, tail=%d",
                     pod_name, namespace, previous, tail)
        args = [pod_name, f"--tail={tail}"]
        if container:
            args += ["--container", container]
        if previous:
            args.append("--previous")
        result = await self._run("logs", args, namespace=namespace, output_json=False)
        logger.debug("[KUBECTL-LOGS] pod=%s previous=%s → success=%s, %d chars",
                     pod_name, previous, result.success, len(result.stdout))
        return result

    async def get_events(
        self,
        namespace: str = "default",
        resource_name: Optional[str] = None,
        resource_type: Optional[str] = None,
    ) -> KubectlResult:
        """Get events, optionally filtered by resource."""
        args = ["events"]
        if resource_name and resource_type:
            args += [f"--field-selector=involvedObject.name={resource_name},involvedObject.kind={resource_type}"]
        elif resource_name:
            args += [f"--field-selector=involvedObject.name={resource_name}"]
        args += ["--sort-by=.lastTimestamp"]
        return await self._run("get", args, namespace=namespace)

    async def get_nodes(self) -> KubectlResult:
        """Get all nodes."""
        return await self._run("get", ["nodes"], namespace=None)

    async def top_pods(self, namespace: str = "default") -> KubectlResult:
        """Get resource usage for all pods in a namespace."""
        return await self._run("top", ["pods"], namespace=namespace, output_json=False)

    async def top_pod(self, pod_name: str, namespace: str = "default") -> KubectlResult:
        """Get current CPU/memory usage for a specific pod."""
        logger.debug("[KUBECTL-TOP] pod=%s, ns=%s", pod_name, namespace)
        result = await self._run("top", ["pods", pod_name], namespace=namespace, output_json=False)
        logger.debug("[KUBECTL-TOP] pod=%s → success=%s  %s",
                     pod_name, result.success, result.stdout.strip()[:80])
        return result

    async def top_nodes(self) -> KubectlResult:
        """Get node resource usage."""
        return await self._run("top", ["nodes"], namespace=None, output_json=False)

    async def get_namespaces(self) -> KubectlResult:
        """List all namespaces."""
        return await self._run("get", ["namespaces"], namespace=None)

    async def cluster_info(self) -> KubectlResult:
        """Get cluster info."""
        return await self._run("cluster-info", [], namespace=None, output_json=False)

    async def get_deployments(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["deployments"], namespace=namespace)

    async def get_services(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["services"], namespace=namespace)

    async def get_ingresses(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["ingresses"], namespace=namespace)

    async def get_pvcs(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["persistentvolumeclaims"], namespace=namespace)

    async def get_configmaps(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["configmaps"], namespace=namespace)

    async def get_hpa(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["horizontalpodautoscalers"], namespace=namespace)

    async def get_statefulsets(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["statefulsets"], namespace=namespace)

    async def get_daemonsets(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["daemonsets"], namespace=namespace)

    async def get_jobs(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["jobs"], namespace=namespace)

    async def get_cronjobs(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["cronjobs"], namespace=namespace)

    async def get_replicasets(self, namespace: str = "default") -> KubectlResult:
        return await self._run("get", ["replicasets"], namespace=namespace)

    async def rollout_status(self, resource_type: str, name: str, namespace: str = "default") -> KubectlResult:
        """Check rollout status (read-only)."""
        # Extra safety: only allow 'status' subcommand
        return await self._run(
            "rollout",
            ["status", f"{resource_type}/{name}"],
            namespace=namespace,
            output_json=False,
        )

    async def version(self) -> KubectlResult:
        """Get kubectl/server version."""
        return await self._run("version", [], namespace=None)

    async def probe_cluster(self) -> bool:
        """Quick check if cluster is reachable."""
        result = await self.get_namespaces()
        return result.success
