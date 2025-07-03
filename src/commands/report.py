"""Typer application to create useful reports."""

import dataclasses
import json
from typing import Annotated, List

import typer
from rich import box
from rich.console import Console
from rich.table import Table

import services.github as github

app = typer.Typer()
console = Console()


@app.command()
def ci(
    team_name: Annotated[
        str,
        typer.Argument(
            help="Name of GitHub team under the Canonical organization.", show_default=False
        ),
    ] = "observability",
    format_json: Annotated[
        bool, typer.Option("--json", help="If True print the CI status as a json.")
    ] = False,
):
    """Print a table with CI status for the specified team under the Canonical org."""

    def _add_status_row(table: Table, repo_name: str, workflows: List[github.WorkflowStatus]):
        """Update the passed table with repository status."""
        if not workflows:
            table.add_row(repo_name, "[dim](no badges)[/dim]")
            return

        status_cell = []
        for workflow in workflows:
            if workflow.status == "success":
                pretty_status = "[green]success[/green]"
            elif workflow.status == "failure":
                pretty_status = f"[red]failure[/red] ([blue link={workflow.url}]link[/blue link])"
            elif workflow.status == "cancelled":
                pretty_status = "[yellow]cancelled[/yellow]"
            else:
                pretty_status = f"[i]{workflow.status}[/i]"
            status_cell.append(f"{workflow.name}: {pretty_status}")

        table.add_row(repo_name, "\n".join(status_cell))

    ci_status = github.team_ci_status(team_name)
    if format_json:
        json_ci_status = {}
        for repo, workflows in ci_status.items():
            json_ci_status[repo] = [dataclasses.asdict(w) for w in workflows]
        console.print(json.dumps(json_ci_status))
    else:
        table = Table(box=box.SIMPLE, safe_box=True)
        table.add_column("Repository", justify="right", style="bold")
        table.add_column("CI status")
        for repo, workflows in ci_status.items():
            _add_status_row(table, repo_name=repo, workflows=workflows)
        console.print(table)


if __name__ == "__main__":
    app()
