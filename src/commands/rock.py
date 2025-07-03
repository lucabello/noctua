"""Typer application to run rock-related commands."""

import logging
import os
import re
from typing import Annotated, List, Optional

import typer
from rich.console import Console

import services.kubernetes as kubernetes
import services.rockcraft as rockcraft

log = logging.getLogger(__name__)
console = Console()

app = typer.Typer()


class InputError(Exception):
    """Exception due to wrong user or file input."""


@app.command()
def status(
    rock_name: Annotated[str, typer.Argument(help="Name of a rock built in OCI Factory.")],
):
    """Print the diff between local tags and the ones currently in OCI Factory."""
    locals = rockcraft.local_tags(os.listdir())
    remote = rockcraft.oci_factory_tags(rock_name=rock_name)

    only_locals = [v for v in locals.keys() if v not in remote]
    if not only_locals:
        console.print(f"[b]{rock_name}[/b] is [green]in sync[/green] with OCI Factory.")
    else:
        console.print(
            f"[b]{rock_name}[/b] is [red]out of sync[/red]: "
            f"the following versions exist locally, but not on OCI Factory: {only_locals}"
        )


@app.command()
def run(
    rock_path: Annotated[
        str,
        typer.Argument(help="Path to a *.rock file."),
    ],
    namespace: Annotated[
        str,
        typer.Option(
            "--namespace",
            help="Kubernetes namespace to run the pod in",
        ),
    ] = "default",
    one_shot: Annotated[
        bool,
        typer.Option("--one-shot", help="Shell into the pod and delete it on exit"),
    ] = False,
):
    """Run a *.rock file in a pod on the local Kubernetes cluster."""
    if not os.path.exists(rock_path):
        raise InputError("The provided rock doesn't exist.")
    regex = re.compile(r"(.*/)*(?P<app>.+)_(?P<version>.+)_(?P<arch>.+)\.rock")
    rock_matches = regex.match(rock_path)
    rock_name = rock_matches.group("app") if rock_matches else "test-rock"
    rock_tag = rock_matches.group("version") if rock_matches else "dev"

    image_uri = rockcraft.push_to_registry(
        path=rock_path, image_name=rock_name, image_tag=rock_tag
    )
    pod_name = f"{rock_name}-{rock_tag.replace('.', '-')}"
    kubernetes.run(pod=pod_name, namespace=namespace, image_uri=image_uri)
    if one_shot:
        kubernetes.open_shell(pod=pod_name, namespace=namespace)
        kubernetes.stop(pod=pod_name, namespace=namespace)


@app.command()
def test(
    rock_path: Annotated[
        str,
        typer.Argument(help="Path to a *.rock file."),
    ],
    namespace: Annotated[
        str,
        typer.Option(
            "--namespace",
            help="Kubernetes namespace to run the pod in",
        ),
    ] = "default",
    goss_path: Annotated[
        Optional[str],
        typer.Option("--goss-file", help="Path to a 'goss.yaml' file", show_default=False),
    ] = None,
    one_shot: Annotated[
        bool,
        typer.Option("--one-shot", help="Delete the pod after running the tests"),
    ] = False,
    is_ci: Annotated[
        bool,
        typer.Option("--ci", help="Run commands without a TTY"),
    ] = False,
):
    """Run a rock and its Goss checks for a specific *.rock file.

    This command expects a 'goss.yaml' in the same folder as the *.rock file,
    to be run from inside the pod. The rock doesn't need to have 'goss' installed.
    """
    if not os.path.exists(rock_path):
        raise InputError("The provided rock doesn't exist.")
    regex = re.compile(r"(.*/)*(?P<app>.+)_(?P<version>.+)_(?P<arch>.+)\.rock")
    rock_matches = regex.match(rock_path)
    rock_name = rock_matches.group("app") if rock_matches else "test-rock"
    rock_tag = rock_matches.group("version") if rock_matches else "dev"
    rock_arch = rock_matches.group("arch") if rock_matches else "amd64"

    if not goss_path:
        goss_path = os.path.join(os.path.dirname(os.path.realpath(rock_path)), "goss.yaml")

    image_uri = rockcraft.push_to_registry(
        path=rock_path, image_name=rock_name, image_tag=rock_tag
    )
    pod_name = f"{rock_name}-{rock_tag.replace('.', '-')}"

    kubernetes.run(pod=pod_name, namespace=namespace, image_uri=image_uri)
    kubernetes.install_goss(pod=pod_name, namespace=namespace, arch=rock_arch)
    kubernetes.install_goss_checks(pod=pod_name, namespace=namespace, path=goss_path)
    kubernetes.run_goss(pod=pod_name, namespace=namespace, is_ci=is_ci)

    if one_shot:
        kubernetes.stop(pod=pod_name, namespace=namespace)


@app.command()
def manifest(
    rock_repo: Annotated[
        str,
        typer.Argument(
            help="Full name of the rock repository (e.g., 'canonical/prometheus-rock')"
        ),
    ],
    commit_sha: Annotated[
        str,
        typer.Option(
            "--commit", help="SHA of the commit on the rock repo to point at", show_default=False
        ),
    ],
    version_list: Annotated[
        List[str],
        typer.Option(
            "--version",
            help="Rock version to build the manifest for (you can pass multiple)",
            show_default=False,
        ),
    ],
    base: Annotated[str, typer.Option(help="Base to append to the tags")] = "22.04",
    risk_track: Annotated[
        str, typer.Option("--risk", help="Risk track to append to the tags")
    ] = "stable",
):
    """Generate the 'image.yaml' manifest for OCI Factory."""
    # Get the tags to apply to each version
    versions_with_tags = rockcraft.local_tags(os.listdir())
    selected_versions = {k: v for k, v in versions_with_tags.items() if k in version_list}
    # Append the -base suffix to the tag
    for version, tags in selected_versions.items():
        selected_versions[version] = [f"{t}-{base}" for t in tags]
    # Validate the rock_repo
    if len(rock_repo.split("/")) != 2:
        raise InputError(
            f"The provided repository '{rock_repo}' is invalid; "
            "please use a full repository name (e.g., 'canonical/prometheus-rock')"
        )
    # Validate the input versions
    for version in version_list:
        if version not in selected_versions:
            raise InputError(
                f"The specified rock version '{version}' doesn't exist locally. "
                f"Existing versions: {list(versions_with_tags.keys())}"
            )

    # Generate the 'image.yaml' manifest
    manifest = rockcraft.oci_factory_manifest(
        repository=rock_repo,
        commit=commit_sha,
        versions_with_tags=selected_versions,
        risk_track=risk_track,
    )
    console.print(manifest)


if __name__ == "__main__":
    app()
