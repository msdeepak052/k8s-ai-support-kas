"""
k8s-ai-support CLI — interactive Kubernetes troubleshooting via natural language.
Built with Typer. Supports --output json|yaml|table and --verbose.
"""

import asyncio
import json
import logging
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.json import JSON
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.syntax import Syntax
from rich.table import Table

from ..agent.graph import get_agent
from ..config.settings import LLMProvider, OutputFormat, get_settings, reset_settings_cache
from ..llm.prompt_templates import format_diagnosis_as_table, format_diagnosis_as_yaml

app = typer.Typer(
    name="k8s-ai-support",
    help="AI-powered Kubernetes troubleshooting agent",
    rich_markup_mode="rich",
    no_args_is_help=False,
)

console = Console()
err_console = Console(stderr=True)


def _setup_logging(verbose: bool, log_level: str = "INFO"):
    """Configure structured JSON logging."""
    import structlog

    level = logging.DEBUG if verbose else getattr(logging, log_level, logging.INFO)

    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer() if not verbose else structlog.dev.ConsoleRenderer(),
        ],
    )


def _print_result(state: dict, output_format: str, verbose: bool):
    """Render the agent result in the requested format."""
    diagnosis = state.get("diagnosis")
    errors = state.get("errors", [])
    warnings = state.get("warnings", [])

    # Show warnings
    for warn in warnings:
        console.print(f"[yellow]⚠ {warn}[/yellow]")

    # Show errors
    for err in errors:
        err_console.print(f"[red]✗ Error: {err}[/red]")

    if not diagnosis:
        console.print("[red]No diagnosis generated. Check errors above.[/red]")
        return

    if output_format == "json":
        console.print(JSON(json.dumps(diagnosis, indent=2)))

    elif output_format == "yaml":
        try:
            yaml_output = format_diagnosis_as_yaml(diagnosis)
            console.print(Syntax(yaml_output, "yaml", theme="monokai"))
        except ImportError:
            console.print("[yellow]PyYAML not installed, falling back to JSON[/yellow]")
            console.print(JSON(json.dumps(diagnosis, indent=2)))

    else:  # table (default)
        table_output = format_diagnosis_as_table(diagnosis)
        console.print(table_output)

    if verbose:
        # Show execution metadata
        console.print()
        meta_table = Table(title="Execution Metadata", show_header=True, header_style="bold blue")
        meta_table.add_column("Metric", style="cyan")
        meta_table.add_column("Value", style="white")
        meta_table.add_row("Steps", " → ".join(state.get("steps_taken", [])))
        meta_table.add_row("Tokens Used", str(state.get("token_count", 0)))
        meta_table.add_row("Execution Time", f"{state.get('execution_time_ms', 0)}ms")
        meta_table.add_row("Cluster Reachable", str(state.get("cluster_reachable", False)))
        console.print(meta_table)


@app.command()
def diagnose(
    query: Optional[str] = typer.Argument(None, help="Natural language query about your K8s issue"),
    namespace: str = typer.Option("default", "--namespace", "-n", help="Kubernetes namespace"),
    resource: Optional[str] = typer.Option(None, "--resource", "-r", help="Specific resource name (pod/deploy/etc)"),
    resource_type: Optional[str] = typer.Option(None, "--type", "-t", help="Resource type (pod, deployment, service, node, pvc)"),
    output: OutputFormat = typer.Option(OutputFormat.TABLE, "--output", "-o", help="Output format"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output with execution details"),
    provider: Optional[LLMProvider] = typer.Option(None, "--provider", "-p", help="LLM provider override"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="LLM model override"),
    interactive: bool = typer.Option(False, "--interactive", "-i", help="Start interactive REPL mode"),
):
    """
    Diagnose Kubernetes issues using AI.

    Examples:

      k8s-ai-support "why is my nginx pod crashing?"

      k8s-ai-support "pod stuck in pending" -n production

      k8s-ai-support "imagepull error" -r nginx-deployment -t deployment

      k8s-ai-support -i   (interactive mode)
    """
    settings = get_settings()
    _setup_logging(verbose, settings.log_level.value)

    # Apply CLI overrides to settings
    if provider:
        import os
        os.environ["K8S_AI_PROVIDER"] = provider.value
        reset_settings_cache()
    if model:
        import os
        os.environ["K8S_AI_MODEL"] = model
        reset_settings_cache()

    if interactive or not query:
        _run_interactive(namespace, output.value, verbose)
        return

    _run_single_query(query, namespace, resource, resource_type, output.value, verbose)


def _run_single_query(query: str, namespace: str, resource: Optional[str], resource_type: Optional[str], output_format: str, verbose: bool):
    """Execute a single query and print results."""
    agent = get_agent()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Analyzing Kubernetes issue...", total=None)

        state = asyncio.run(
            agent.run(
                query=query,
                namespace=namespace,
                resource_name=resource,
                resource_type=resource_type,
                output_format=output_format,
                verbose=verbose,
            )
        )

    _print_result(state, output_format, verbose)


def _run_interactive(namespace: str, output_format: str, verbose: bool):
    """Start interactive REPL mode."""
    console.print(Panel(
        "[bold cyan]k8s-ai-support[/bold cyan] — Interactive Kubernetes Troubleshooting\n"
        "Type your question, or use commands:\n"
        "  [yellow]/ns <namespace>[/yellow] — switch namespace\n"
        "  [yellow]/format json|yaml|table[/yellow] — change output format\n"
        "  [yellow]/quit[/yellow] — exit",
        title="Interactive Mode",
        border_style="cyan",
    ))

    agent = get_agent()
    current_ns = namespace
    current_format = output_format

    while True:
        try:
            query = console.input(f"\n[bold cyan]k8s-ai[/bold cyan] [{current_ns}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Goodbye![/yellow]")
            break

        if not query:
            continue

        # Handle commands
        if query.lower() in ("/quit", "/exit", "/q"):
            console.print("[yellow]Goodbye![/yellow]")
            break
        elif query.lower().startswith("/ns "):
            current_ns = query[4:].strip()
            console.print(f"[green]Switched to namespace: {current_ns}[/green]")
            continue
        elif query.lower().startswith("/format "):
            fmt = query[8:].strip().lower()
            if fmt in ("json", "yaml", "table"):
                current_format = fmt
                console.print(f"[green]Output format: {current_format}[/green]")
            else:
                console.print("[red]Invalid format. Use: json, yaml, table[/red]")
            continue

        # Run query
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            transient=True,
        ) as progress:
            progress.add_task("Analyzing...", total=None)
            state = asyncio.run(agent.run(
                query=query,
                namespace=current_ns,
                output_format=current_format,
                verbose=verbose,
            ))

        _print_result(state, current_format, verbose)


@app.command()
def mcp():
    """Start the MCP (Model Context Protocol) server for IDE integration."""
    from .mcp_server import MCPServer
    server = MCPServer()
    asyncio.run(server.start())


@app.command()
def version():
    """Show version information."""
    from importlib.metadata import version as pkg_version
    try:
        v = pkg_version("k8s-ai-support")
    except Exception:
        v = "0.1.0-dev"
    settings = get_settings()
    console.print(f"k8s-ai-support v{v}")
    console.print(f"Provider: {settings.provider.value} | Model: {settings.model}")


@app.command()
def check():
    """Check cluster connectivity and LLM provider configuration."""
    settings = get_settings()
    console.print("[bold]Configuration Check[/bold]")

    # K8s connectivity
    from ..tools.kubectl_wrapper import KubectlWrapper
    kubectl = KubectlWrapper()

    async def _check():
        return await kubectl.probe_cluster()

    reachable = asyncio.run(_check())
    status = "[green]✓ Connected[/green]" if reachable else "[red]✗ Not reachable[/red]"
    console.print(f"Kubernetes cluster: {status}")

    # LLM provider
    key_map = {
        LLMProvider.OPENAI: settings.openai_api_key,
        LLMProvider.GEMINI: settings.gemini_api_key,
        LLMProvider.CLAUDE: settings.anthropic_api_key,
        LLMProvider.OLLAMA: "N/A (local)",
    }
    key_status = key_map.get(settings.provider)
    if key_status and key_status != "N/A (local)":
        key_display = f"{key_status[:8]}...{key_status[-4:]}" if len(key_status) > 12 else "***"
        console.print(f"LLM Provider: {settings.provider.value} | Model: {settings.model} | Key: {key_display}")
    else:
        console.print(f"LLM Provider: {settings.provider.value} | Model: {settings.model}")

    if not reachable:
        console.print("[yellow]Tip: Set KUBECONFIG or ensure kubectl is configured[/yellow]")


def main():
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
