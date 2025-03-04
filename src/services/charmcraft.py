"""Wraps and extend `charmcraft` commands."""

import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import sh
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

# pyright: reportAttributeAccessIssue=false


class InputError(Exception):
    """Exception due to wrong user or file input."""


class CharmhubError(Exception):
    """Triggered by a failure in a Charmhub interaction."""


@dataclass
class CharmLibrary:
    """Mapping of a Charm library, based on Charmcraft's `list-lib` response object."""

    charm_name: str
    library_name: str
    api: int
    patch: int
    library_id: Optional[str] = None
    content_hash: Optional[str] = None

    @property
    def full_name(self) -> str:
        """Return the full name of the library, as used in `charmcraft fetch-lib`."""
        return f"charms.{self.charm_name.replace('-', '_')}.v{self.api}.{self.library_name}"

    @classmethod
    def from_file(cls, filename: str) -> "CharmLibrary":
        """Create a CharmLibrary object from a library file.

        Args:
            filename: Path to the library, relative to the charm root folder.
        """
        if not filename.startswith("lib"):
            raise InputError(
                f"Can't construct CharmLibrary from path {filename}: "
                "the path should start with 'lib/'."
            )

        filename_matches = re.compile(r"^lib/charms/(.+)/v\d+/(.+)\.py").match(filename)
        if not filename_matches:
            raise InputError(
                f"Can't parse 'charm_name' and 'library_name' from the path: {filename}"
            )
        charm, library = filename_matches.group(1), filename_matches.group(2)
        with open(filename, "r") as f:
            contents = f.read()
            libapi_matches = re.search(r"LIBAPI = (\d+)", contents)
            libpatch_matches = re.search(r"LIBPATCH = (\d+)", contents)
            if not libapi_matches or not libpatch_matches:
                raise InputError(f"Can't parse LIBAPI and LIBPATCH from the file: {filename}")
            libapi, libpatch = libapi_matches.group(1), libpatch_matches.group(1)

        return cls(
            charm_name=charm.replace("_", "-"),
            library_name=library,
            api=int(libapi),
            patch=int(libpatch),
        )

    @classmethod
    def from_charmhub(cls, charm_name: str) -> "Dict[str, CharmLibrary]":
        """Create a collection of CharmLibrary objects from `charmcraft list-lib`.

        The Dict is indexed by the "Charmhub path" (e.g., 'charms.catalogue_k8s.v1.catalogue').

        Output from `charmcraft list-lib`:
            [
                {
                    "charm_name": "catalogue-k8s",
                    "library_name": "catalogue",
                    "library_id": "fa28b361293b46668bcd1f209ada6983",
                    "api": 1,
                    "patch": 0,
                    "content_hash": "<some_hash>"
                }
            ]
        """
        charmhub_libraries = json.loads(
            sh.charmcraft("list-lib", charm_name, format="json", _tty_out=False)
        )
        libraries = {}
        for item in charmhub_libraries:
            library = CharmLibrary(
                charm_name=item["charm_name"],
                library_name=item["library_name"],
                api=item["api"],
                patch=item["patch"],
                library_id=item["library_id"],
                content_hash=item["content_hash"],
            )
            libraries[library.full_name] = library

        return libraries

    @classmethod
    def from_charmhub_with_name(cls, charm_name: str, library_name: str) -> "CharmLibrary":
        """Create a CharmLibrary object from the information on Charmhub.

        Args:
            charm_name: Name of the charm owning the library.
            library_name: Name of the library (e.g., 'ingress').
        """
        libraries: "Dict[str, CharmLibrary]" = cls.from_charmhub(charm_name)
        matching_libraries = [
            lib for lib in libraries.values() if lib.library_name == library_name
        ]
        if not matching_libraries:
            raise CharmhubError(
                f"The charm {charm_name} has no library named {library_name} on Charmhub."
            )
        return matching_libraries[0]


@dataclass
class CharmResource:
    """Helper class to represent a charm resource."""

    name: str
    revision: Optional[int] = None
    upstream_source: Optional[str] = None  # as specified in a charm's metadata

    def __str__(self):
        """Return a JSON representation of the CharmResource object."""
        charm_resource: Dict[str, Any] = {"name": self.name}
        if self.revision:
            charm_resource["revision"] = self.revision
        if self.upstream_source:
            charm_resource["upstream_source"] = self.upstream_source
        return json.dumps(charm_resource)


@dataclass
class CharmUpload:
    """Metadata obtained after uploading a charm with `charmcraft upload`."""

    name: str
    revision: int
    resources: List[CharmResource]

    def __str__(self):
        """Return a JSON representation of the CharmUpload object."""
        return json.dumps(
            {
                "name": self.name,
                "revision": self.revision,
                "resources": [json.loads(str(r)) for r in self.resources],
            }
        )


@dataclass
class CharmChannel:
    """Helper class representing a release channel (e.g, 'latest/stable')."""

    name: str  # full channel namge (e.g. 'latest/stable')
    _REGEX: re.Pattern[str] = re.compile(r"^(\S+)\/(edge|beta|candidate|stable)$")

    @property
    def track(self) -> str:
        """Return the track section of a channel."""
        match = self._REGEX.match(self.name)
        if match is None:
            raise InputError(f"Failed to parse track from '{self.name}': not a valid channel")
        return match.group(1)

    @property
    def risk(self) -> str:
        """Return the risk section of a channel."""
        match = self._REGEX.match(self.name)
        if match is None:
            raise InputError(f"Failed to parse risk from '{self.name}': not a valid channel")
        return match.group(2)

    @property
    def next_risk_channel(self) -> "CharmChannel":
        """Return a CharmChannel with the same track and a lower risk."""
        risk_table = {
            "edge": "beta",
            "beta": "candidate",
            "candidate": "stable",
            "stable": "stable",
        }
        return CharmChannel(f"{self.track}/{risk_table[self.risk]}")


@dataclass
class ChannelStatus:
    """Release status of a channel (e.g., 'latest/stable').

    It specifies the charm revision for a certain channel and base.
    """

    name: str
    status: Literal["open", "closed"]
    base_version: str
    base_arch: str
    revision: int
    resources: List[CharmResource]


@dataclass
class BaseStatus:
    """Release status of all the channels for a certain base (e.g., '22.04/amd64')."""

    version: str  # Base version (e.g., '22.04')
    arch: str  # Base architecture (e.g., 'amd64')
    channels: Dict[str, ChannelStatus]  # Map channel names to their release status

    @property
    def name(self):
        """Return a string representation of the base."""
        return f"{self.version}/{self.arch}"


@dataclass
class TrackStatus:
    """Release status of all the channels and bases in a track (e.g., 'latest')."""

    name: str
    bases: Dict[str, BaseStatus]  # Map base names (e.g. '22.04/amd64') to their release status


console = Console()


def status(charm: str) -> Dict:
    """Return the output of `charmcraft status` as a dictionary."""
    charmcraft_status = sh.charmcraft.status(charm, format="json", _tty_out=False)
    return json.loads(charmcraft_status)


def metadata() -> Dict:
    """Return the charm metadata from metadata.yaml or charmcraft.yaml as a dictionary."""
    if os.path.exists("metadata.yaml"):
        metadata_file = "metadata.yaml"
    elif os.path.exists("charmcraft.yaml"):
        metadata_file = "charmcraft.yaml"
    else:
        raise InputError(
            "No 'metadata.yaml' or 'charmcraft.yaml' file found in the current working directory."
            "Please run the command from the charm folder."
        )

    with open(metadata_file, "r") as f:
        return yaml.safe_load(f)


def release_status(charm: str) -> Dict[str, TrackStatus]:
    """Summarize `charmcraft status` information.

    Returns:
        A map of TrackStatus by the track name, summarizing `charmcraft status` information.

    Example:
        {
            'latest': TrackStatus(
                name='latest',
                bases={
                    '20.04/amd64': BaseStatus(
                        version='20.04',
                        arch='amd64',
                        channels={
                            'latest/stable': ChannelStatus(
                                name='latest/stable',
                                status='open',
                                base_version='20.04',
                                base_arch='amd64',
                                revision=1,
                                resources=[
                                    CharmResource(
                                        name='blackbox-exporter-image',
                                        revision=1,
                                        upstream_source=None
                                    )
                                ]
                            ),
                            ...
                        }
                    ),
                    ...
                }
            )
        }
    """
    charmcraft_status = status(charm)
    tracks_status: Dict[str, TrackStatus] = {}
    for track_mappings in charmcraft_status:
        bases: Dict[str, BaseStatus] = {}
        # get the track (e.g., 'latest')
        track = track_mappings["track"]
        for mapping in track_mappings["mappings"]:  # for each base
            channels: Dict[str, ChannelStatus] = {}
            base_version = mapping["base"]["channel"]
            base_arch = mapping["base"]["architecture"]
            for release in mapping["releases"]:  # for each channel
                resources = release["resources"] or []
                # get the information the specific channel (e.g, 'latest/edge')
                channel_status = ChannelStatus(
                    name=release["channel"],
                    status=release["status"],
                    base_version=base_version,
                    base_arch=base_arch,
                    revision=release["revision"] or -1,
                    resources=[
                        CharmResource(name=res["name"], revision=res["revision"])
                        for res in resources
                    ],
                )
                channels[channel_status.name] = channel_status
            base_status = BaseStatus(version=base_version, arch=base_arch, channels=dict(channels))
            bases[base_status.name] = base_status

        tracks_status[track] = TrackStatus(name=track, bases=bases)

    return tracks_status


def upload(
    charm_name: str, path: str | Path, quiet: bool = False, dry_run: bool = False
) -> CharmUpload:
    """Upload a local '.charm' file to Charmhub.

    Args:
        charm_name: The charm name registered in Charmhub.
        path: The path to the local '.charm' file to upload.
        quiet: Don't print anything to stdout.
        dry_run: If True, only print the uploads.

    Returns:
        CharmUpload describing the uploaded charm and resources revisions.

    Example:
        CharmUpload(
            name='o11y-tester',
            reivision=10,
            resources=[
                CharmResource(
                    name='httpbin-image',
                    revision=1,
                    upstream_source='...'
                ),
                ...
            ]
        )
    """
    if not os.path.exists(path):
        raise InputError("The supplied '.charm' file doesn't exist.")

    # Upload the charm
    if dry_run:
        fake_upload = CharmUpload(name=charm_name, revision=0, resources=[])
        console.print(f"[yellow](dry_run)[/yellow] charmcraft upload {path} --format=json")
        # console.print(
        #     f"[yellow](dry_run)[/yellow] [b]{charm_name}[/b]: charm uploaded {fake_upload}"
        # )
        return fake_upload
    # `charmcraft upload` output: {"revision": <int>}
    try:
        output = sh.charmcraft.upload(path, format="json", _tty_out=False)
    except sh.ErrorReturnCode as e:
        try:
            errors = json.loads(e.stdout)["errors"]
        except (json.JSONDecodeError, KeyError):
            console.print(e.stderr)
            raise
        else:
            if len(errors) != 1:
                console.print(e.stderr)
                raise
            error = errors[0]
            if error.get("code") != "review-error":
                console.print(e.stderr)
                raise
            match = re.fullmatch(
                r".*?Revision of the existing package is: (?P<revision>[0-9]+)",
                error.get("message", ""),
            )
            if not match:
                console.print(e.stderr)
                raise
            revision = int(match.group("revision"))
            console.print(f"Warning: {path=} already uploaded. Using existing {revision=}")
    else:
        revision: int = json.loads(output)["revision"]
        console.print(f"Uploaded charm {revision=}")

    # Upload the resources
    charm_resources = metadata().get("resources", {})  # machine charms have no resources
    uploaded_resources: List[CharmResource] = []
    for resource_name in charm_resources.keys():
        upstream_source = charm_resources[resource_name][
            "upstream-source"
        ]  # e.g., ubuntu/prometheus:2-22.04
        if dry_run:
            console.print(
                f"[yellow](dry_run)[/yellow] charmcraft upload-resource "
                f"{charm_name} {resource_name} --image=docker://{upstream_source} "
                "--format=json"
            )
            continue
        # `upload-resource` output: {"revision": <int>}
        upload_result = json.loads(
            sh.charmcraft(
                "upload-resource",
                charm_name,
                resource_name,
                image=f"docker://{upstream_source}",
                format="json",
                _tty_out=False,
            )
        )
        resource = CharmResource(
            name=resource_name, revision=upload_result["revision"], upstream_source=upstream_source
        )
        uploaded_resources.append(resource)
        if not quiet:
            console.print(f"[b]{charm_name}[/b]: resource uploaded {resource}")

    # Build the CharmUpload object and return it
    uploaded_charm = CharmUpload(name=charm_name, revision=revision, resources=uploaded_resources)
    if not quiet:
        console.print(f"[b]{charm_name}[/b]: charm uploaded {uploaded_charm}")

    return uploaded_charm


def release(
    charm: str,
    channel: str,
    revision: int,
    resources: List[str],
    quiet: bool = False,
    dry_run: bool = False,
) -> None:
    """Release a charm using `charmcraft release`.

    Args:
        charm: The charm name registered in Charmhub.
        channel: The channel to release the charm to (e.g., 'latest/edge')
        revision: The charm revision to release
        resources: List of resources to attach in 'resource:revision' format (e.g., 'loki-image:4')
        quiet: Don't print anything to stdout.
        dry_run: If True only print the eventual release without executing it.
    """
    resources_args = [f"--resource={r}" for r in resources]
    if dry_run:
        console.print(
            f"[yellow](dry run)[/yellow] charmcraft release "
            f"{charm} {' '.join(resources_args)} --channel={channel} "
            f"--revision={revision}"
        )
    else:
        sh.charmcraft.release(
            charm, *resources_args, channel=channel, revision=revision, _tty_out=False
        )
        if not quiet:
            console.print(f"Released [b]{charm}[/b] {revision} to {channel}")


def promote(charm: str, source: str, target: str, dry_run: bool = False):
    """Promote a charm revision from a channel to another one."""
    source_channel = CharmChannel(source)
    releases = release_status(charm)
    track_status = releases[source_channel.track]
    # Execute the promotion for all bases in order, starting from the oldest ones
    for base, base_status in track_status.bases.items():
        source_status = base_status.channels[source]
        target_status = base_status.channels[target]
        if source_status.status == "open" and target_status.status == "open":
            resources_args = [f"--resource={r.name}:{r.revision}" for r in source_status.resources]
            if dry_run:
                console.print(
                    f"[yellow](dry run)[/yellow] "
                    f"charmcraft release {charm} {' '.join(resources_args)} "
                    f"--channel={target} --revision={source_status.revision}"
                )
                console.print(
                    f"[yellow](dry run)[/yellow] "
                    f"Promoted [b]{charm}[/b] from {source} ({source_status.revision}) "
                    f"to {target} ({target_status.revision}) ({base})"
                )
            else:
                sh.charmcraft.release(
                    charm,
                    *resources_args,
                    channel=target,
                    revision=source_status.revision,
                )
                console.print(
                    f"Promoted [b]{charm}[/b] from {source} ({source_status.revision}) "
                    f"to {target} ({target_status.revision}) ({base})"
                )
        else:
            console.print(f"{target} for [b]{charm}[/b] is closed. Skipping promotion.")


def local_charm_libraries() -> Dict[str, CharmLibrary]:
    """Return the charm libraries used by the charm in the current working directory.

    Returns:
        A dictionary mapping libraries' full names to their metadata:
        {
            'charms.catalogue_k8s.v1.catalogue': CharmLibrary(
                charm_name='catalogue-k8s',
                library_name='catalogue',
                api=1,
                patch=0,
                library_id=None,
                content_hash=None,
            ),
            ...
        }
    """
    if not os.path.exists("lib/charms"):
        raise InputError(
            "No 'lib/charms' folder detected. Make sure to run this from a charm folder."
        )

    libraries = {}
    # List all the charm libraries
    for root, _, files in os.walk("lib/charms"):
        # Avoid __pycache__, similar directories, and dirs with no files
        charm_library_regex = re.compile(r"^lib/charms/([^/]+)/v(\d+)$")
        matches = charm_library_regex.match(root)
        if not matches or not files:
            continue

        for filename in files:
            if not filename.endswith(".py"):
                continue
            library: CharmLibrary = CharmLibrary.from_file(os.path.join(root, filename))
            libraries[library.full_name] = library

    return libraries


def outdated_charm_libraries(minor: bool = True, major: bool = True) -> Dict[str, CharmLibrary]:
    """Return the charm libraries that are outdated.

    The target charm must be in the current working directory.

    Returns:
        A dictionary mapping libraries' full names to their metadata:
        {
            'charms.tempo_k8s.v2.tracing': CharmLibrary(
                charm_name='tempo-k8s',
                library_name='tracing',
                api=2,
                patch=8,
                library_id='12977e9aa0b34367903d8afeb8c3d85d',
                content_hash='59ff46b0f3818bb82fe16a9eaf5b47357a578c54366fc284ed92d06094be52e8'
            ),
            ...
        }
    """
    outdated_libraries = {}
    locals = local_charm_libraries()
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=20),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        SpinnerColumn(spinner_name="line"),
        TextColumn("{task.fields[library]}"),
        transient=True,
    )
    progress_task = progress.add_task("Checking", total=len(locals.values()), library="")
    with progress:
        for local_library in locals.values():
            progress.update(progress_task, advance=1, library=local_library.full_name)
            # Get the info on the version of the charm library on Charmhub
            charmhub_library = CharmLibrary.from_charmhub_with_name(
                charm_name=local_library.charm_name, library_name=local_library.library_name
            )

            is_outdated = False
            # If the library has a major update and we are interested
            if local_library.api < charmhub_library.api and major:
                is_outdated = True
            # If the library has a minor update and we are interested
            if (
                local_library.api == charmhub_library.api
                and local_library.patch < charmhub_library.patch
                and minor
            ):
                is_outdated = True
            if is_outdated:
                outdated_libraries[charmhub_library.full_name] = charmhub_library
    return outdated_libraries


def publish_charm_libraries(dry_run: bool = False):
    """Publish the charm libraries that belong to the charm.

    Args:
        dry_run: If True only print the libraries that would be published.
    """
    charm_name = metadata()["name"]
    libraries = {k: v for k, v in local_charm_libraries().items() if v.charm_name == charm_name}
    if not libraries:
        console.print(f"[b]{charm_name}[/b] doesn't own any library.")
    for library in libraries.values():
        if dry_run:
            console.print(
                "[yellow](dry_run)[/yellow] "
                f"charmcraft publish-lib {library.full_name} --format=json"
            )
            console.print(
                "[yellow](dry_run)[/yellow] "
                f"[b]v{library.api}/{library.library_name}[/b] patch:{library.patch})"
                "has been published"
            )
            continue

        result = json.loads(
            sh.charmcraft("publish-lib", library.full_name, format="json", _tty_out=False)
        )
        error_message = result["error_message"]
        if error_message:
            if "is already updated" in error_message:
                console.print(f"Library {library.full_name} is already updated in Charmhub.")
                return
            if "is the same than in Charmhub but content is different" in error_message:
                raise CharmhubError(error_message)
            if "LIBPATCH number was incorrectly incremented" in error_message:
                raise CharmhubError(error_message)
            if "has a wrong LIBPATCH number, it's too high" in error_message:
                raise CharmhubError(error_message)
        console.print(
            f"[b]{library.library_name}[/b] v{library.api}.{library.patch} has been published"
        )


def pack(filename: str):
    """Pack a charm from the specified 'charmcraft.yaml' file.

    This function assumes the working directory is the root of a charm.

    Args:
        filename: Name of the 'charmcraft.yaml' file to use to pack the charm.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        backup_file = os.path.join(temp_dir, "charmcraft.yaml.bak")
        # Backup charmcraft.yaml if it exists
        if os.path.exists("charmcraft.yaml"):
            sh.cp("charmcraft.yaml", backup_file)
        # Use the selected file to pack the charm
        if filename != "charmcraft.yaml":
            sh.cp(filename, "charmcraft.yaml")

        try:
            # Run `charmcraft pack`
            sh.charmcraft.pack(_in=sys.stdin, _out=sys.stdout, _err=sys.stderr)
        finally:
            # Restore the backup charmcraft.yaml if a backup has been created
            if os.path.exists(backup_file):
                sh.cp(backup_file, "charmcraft.yaml")
            # If there was no charmcraft.yaml and we created one, remove it
            elif filename != "charmcraft.yaml":
                os.unlink("charmcraft.yaml")
