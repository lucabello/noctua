"""Wraps and extend `rockcraft` commands."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

import requests
import sh
import yaml
from packaging.version import Version

# pyright: reportAttributeAccessIssue=false


class InputError(Exception):
    """Exception due to wrong user or file input."""


class GitHubError(Exception):
    """Trigger by failed interactions with GitHub."""


def local_tags(version_folders: List[str]) -> Dict[str, List[str]]:
    """Compute the tags that would be assigned to each rock version.

    This assumes the working directory is the root of the rock repo,
    and that it contains folders with version names. Folders that are not
    named after semantic versions are be ignored.

    Args:
        version_folders: List of folders named after semantic versions.

    Returns:
        A dictionary with structure {version: list(tags)}.
    """
    # Versions should be major.minor or major.minor.patch
    version_regex = re.compile(r"^\d+\.\d+(\.\d+)?$")
    versions = list(filter(lambda x: version_regex.match(x), version_folders))

    if not versions:
        raise InputError("There are no versioned folders in the current working directory.")

    # Sort the versions semantically
    versions.sort(key=Version)
    tags = {}
    for version_str in versions:
        version_search = re.search(version_regex, version_str)
        has_patch = True if version_search and version_search.group(1) else False

        version = Version(version_str)
        major_tag = f"{version.major}"
        minor_tag = f"{version.major}.{version.minor}"
        patch_tag = f"{version.major}.{version.minor}.{version.micro}" if has_patch else None
        # Add the tags if they don't exist
        if major_tag not in tags:
            tags[major_tag] = version_str
        if minor_tag not in tags:
            tags[minor_tag] = version_str
        if patch_tag and patch_tag not in tags:
            tags[patch_tag] = version_str
        # Compare the versions if the tag already exists
        if version > Version(tags[major_tag]):
            tags[major_tag] = version_str
        if version > Version(tags[minor_tag]):
            tags[minor_tag] = version_str

    tags_per_version = {}
    for v in versions:
        tag_list = [tag for tag, ver in tags.items() if ver == v]
        tags_per_version[v] = tag_list

    return tags_per_version


def oci_factory_tags(rock_name: str) -> List[str]:
    """Return the tags currently built in OCI Factory (from _releases.json).

    Args:
        rock_name: The rock name as it appears in OCI Factory (e.g., 'prometheus').
    """
    if "-rock" in rock_name:
        raise InputError(f"{rock_name} should be the rock name, not the repository.")
    releases_url = f"https://raw.githubusercontent.com/canonical/oci-factory/main/oci/{rock_name}/_releases.json"
    r = requests.get(releases_url)
    if r.status_code == 404:
        return []

    if r.status_code != 200:
        raise GitHubError(
            f"Error getting info from OCI Factory for {rock_name}: "
            f"request returned {r.status_code}"
        )

    # raw_tags has the following format:
    # {
    #     '2.8.4-22.04': {
    #         'stable': {'target': '84'},
    #         'candidate': {'target': '84'},
    #         'beta': {'target': '84'},
    #         'edge': {'target': '84'},
    #         'end-of-life': '2025-03-14T00:00:00Z'
    #     },
    #     ...
    # }
    raw_tags = json.loads(r.text)
    # Remove the -base suffix
    tags = [t.split("-")[0] for t in raw_tags.keys()]
    tags.sort(key=Version)
    return tags


def oci_factory_manifest(
    repository: str,
    commit: str,
    versions_with_tags: Dict[str, List[str]],
    risk_track: str = "stable",
) -> str:
    """Generate an OCI Factory manifest (i.e., the 'image.yaml' file).

    This assumes that the rock repo is structured with versioned folders,
    each containing a 'rockcraft.yaml' file; also, the current working directory
    must be a rock repo.

    Args:
        repository: Full name of the rock repo (e.g., 'canonical/prometheus-rock').
        commit: SHA of the commit (in the rock repo) to point at.
        versions_with_tags: Dict of {version: [tags]} to add to the manifest.
        risk_track: Track that should be set in the OCI manifest.

    Returns:
        The generated 'image.yaml', formatted according to OCI Factory standards.
    """

    class CompliantDumper(yaml.Dumper):
        def increase_indent(self, flow=True, indentless=False):
            """Force indent when executing dump."""
            return super().increase_indent(flow, False)

    end_of_life_date = datetime.now() + timedelta(days=365 / 4)  # EOL is 3 months by default
    end_of_life_patch_date = datetime.now() - timedelta(days=1)  # for patch releases
    end_of_life = f"{end_of_life_date.strftime('%Y-%m-%d')}T00:00:00Z"
    end_of_life_patch = f"{end_of_life_patch_date.strftime('%Y-%m-%d')}T00:00:00Z"

    manifest = {}
    manifest["version"] = 1
    manifest["upload"] = []
    for version, tags in versions_with_tags.items():
        upload_item = {}
        upload_item["source"] = repository
        upload_item["commit"] = commit
        upload_item["directory"] = version
        upload_item["release"] = {}
        for tag in tags:
            # for patch tags, we set end-of-life to be "yesterday"
            is_tag_with_patch = len(tag.split("-")[0].split(".")) == 3
            upload_item["release"][tag] = {
                "end-of-life": end_of_life_patch if is_tag_with_patch else end_of_life,
                "risks": [risk_track],
            }
        manifest["upload"].append(upload_item)

    return yaml.dump(
        manifest, Dumper=CompliantDumper, default_flow_style=False, sort_keys=False, indent=2
    ).strip()


def push_to_registry(
    path: str | Path, image_name: str, image_tag: str, registry: str = "localhost:32000"
) -> str:
    """Push a .rock file to a docker registry.

    The rock is pushed by default to the local registry, with an image

    Args:
        path: Path to the .rock file.
        image_name: Name of the pushed image.
        image_tag: Tag to apply to the pushed image.
        registry: URL of the registry to push the image to (defaults to local registry).

    Returns:
        A URI of the rock in the registry (e.g., `docker://localhost:32000/image:tag`).
    """
    skopeo: sh.Command = sh.Command("rockcraft.skopeo").bake(insecure_policy=True)
    image_uri = f"{registry}/{image_name}:{image_tag}"
    skopeo.copy(f"oci-archive:{path}", f"docker://{image_uri}", dest_tls_verify="false")
    return image_uri
