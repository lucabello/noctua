from datetime import datetime, timedelta
from typing import Dict, List, Literal
from unittest.mock import MagicMock, patch

import pytest
import yaml

import services.rockcraft as rockcraft
import tests.constants as constants


@pytest.mark.parametrize(
    "folders, expected",
    [
        (
            ["1.0.0", "1.0.1", "1.1.0"],
            {"1.0.0": ["1.0.0"], "1.0.1": ["1.0", "1.0.1"], "1.1.0": ["1", "1.1", "1.1.0"]},
        ),
        (
            ["1.0", "1.1"],
            {"1.0": ["1.0"], "1.1": ["1", "1.1"]},
        ),
    ],
)
def test_local_tags(folders: List[str], expected: Dict[str, List[str]]):
    assert rockcraft.local_tags(folders) == expected
    with pytest.raises(rockcraft.InputError):
        rockcraft.local_tags([])
    with pytest.raises(rockcraft.InputError):
        rockcraft.local_tags(["no-version", "here"])


def test_local_tags_with_major_minor_folder(tmp_path):
    """A 'major.minor' folder should resolve its real version from rockcraft.yaml."""
    folder = tmp_path / "2.2"
    folder.mkdir()
    (folder / "rockcraft.yaml").write_text(yaml.dump({"name": "pyroscope", "version": "2.2.1"}))

    resolved = rockcraft.resolve_version_folders(["2.2"], base_path=tmp_path)
    assert resolved == {"2.2.1": "2.2"}

    tags = rockcraft.local_tags(["2.2"], base_path=tmp_path)
    assert tags == {"2.2.1": ["2", "2.2", "2.2.1"]}


def test_local_tags_mixed_layout(tmp_path):
    """Old-style full-semver folders and new-style 'major.minor' folders can coexist."""
    old_folder = tmp_path / "1.18.0"
    old_folder.mkdir()

    new_folder = tmp_path / "2.2"
    new_folder.mkdir()
    (new_folder / "rockcraft.yaml").write_text(
        yaml.dump({"name": "pyroscope", "version": "2.2.1"})
    )

    resolved = rockcraft.resolve_version_folders(["1.18.0", "2.2"], base_path=tmp_path)
    assert resolved == {"1.18.0": "1.18.0", "2.2.1": "2.2"}

    tags = rockcraft.local_tags(["1.18.0", "2.2"], base_path=tmp_path)
    assert tags == {
        "1.18.0": ["1", "1.18", "1.18.0"],
        "2.2.1": ["2", "2.2", "2.2.1"],
    }


def test_local_tags_folder_without_rockcraft_yaml_falls_back_to_folder_name(tmp_path):
    """A folder with no rockcraft.yaml should fall back to using its name as the version."""
    folder = tmp_path / "2.2"
    folder.mkdir()

    resolved = rockcraft.resolve_version_folders(["2.2"], base_path=tmp_path)
    assert resolved == {"2.2": "2.2"}


def test_oci_factory_tags():
    with patch("requests.get", MagicMock()) as get_mock:
        get_mock.return_value.status_code = 404
        assert rockcraft.oci_factory_tags("something") == []
        # Test '-rock' nor in the name
        with pytest.raises(rockcraft.InputError):
            rockcraft.oci_factory_tags("some-rock")
        with pytest.raises(rockcraft.InputError):
            rockcraft.oci_factory_tags("canonical/some-rock")

        get_mock.return_value.status_code = 300
        with pytest.raises(rockcraft.GitHubError):
            rockcraft.oci_factory_tags("something")

        get_mock.return_value.status_code = 200
        get_mock.return_value.text = constants.ROCKCRAFT_OCI_RELEASES["prometheus"]
        assert rockcraft.oci_factory_tags("something") == ["2.45.0", "2.45"]


def test_oci_factory_manifest():
    repository = "canonical/prometheus-rock"
    commit = "abcdef123"
    versions_with_tags = {"1.0.0": ["1.0.0"], "1.0.1": ["1", "1.0", "1.0.1"]}
    end_of_life_date = datetime.now() + timedelta(days=91)
    end_of_life = f"{end_of_life_date.strftime('%Y-%m-%d')}T00:00:00Z"

    # Default support is "minor", so patch tags are omitted entirely.
    # Version "1.0.0" only has a patch-level tag, so it's skipped.
    expected_manifest = {
        "version": 2,
        "upload": [
            {
                "source": "canonical/prometheus-rock",
                "commit": "abcdef123",
                "directory": "1.0.1",
                "release": {
                    "1": {"end-of-life": end_of_life, "risks": ["stable"]},
                    "1.0": {"end-of-life": end_of_life, "risks": ["stable"]},
                },
            },
        ],
    }
    manifest: Dict = yaml.safe_load(
        rockcraft.oci_factory_manifest(repository, commit, versions_with_tags)
    )
    # Make sure all the uploads point to the same repo and commit
    assert len({x["source"] for x in manifest["upload"]}) == 1  # pyright: ignore
    assert len({x["commit"] for x in manifest["upload"]}) == 1  # pyright: ignore

    assert manifest == expected_manifest


def test_oci_factory_manifest_with_version_folders():
    """When a version's folder differs from its version, 'directory' should use the folder."""
    repository = "canonical/pyroscope-rock"
    commit = "abcdef123"
    versions_with_tags = {"2.2.1": ["2", "2.2", "2.2.1"]}
    version_folders = {"2.2.1": "2.2"}

    manifest: Dict = yaml.safe_load(
        rockcraft.oci_factory_manifest(
            repository,
            commit,
            versions_with_tags,
            version_folders=version_folders,
        )
    )
    assert manifest["upload"][0]["directory"] == "2.2"  # pyright: ignore


@pytest.mark.parametrize("risk_track", ["stable", "edge"])
def test_oci_factory_manifest_with_risk_track(risk_track):
    repository = "canonical/prometheus-rock"
    commit = "abcdef123"
    versions_with_tags = {"1.0.0": ["1.0.0"], "1.0.1": ["1", "1.0", "1.0.1"]}
    end_of_life_date = datetime.now() + timedelta(days=91)
    end_of_life = f"{end_of_life_date.strftime('%Y-%m-%d')}T00:00:00Z"

    # Default support is "minor", so patch tags are omitted.
    expected_manifest = {
        "version": 2,
        "upload": [
            {
                "source": "canonical/prometheus-rock",
                "commit": "abcdef123",
                "directory": "1.0.1",
                "release": {
                    "1": {"end-of-life": end_of_life, "risks": [risk_track]},
                    "1.0": {"end-of-life": end_of_life, "risks": [risk_track]},
                },
            },
        ],
    }
    manifest: Dict = yaml.safe_load(
        rockcraft.oci_factory_manifest(repository, commit, versions_with_tags, risk_track)
    )
    # Make sure all the uploads point to the same repo and commit
    assert len({x["source"] for x in manifest["upload"]}) == 1  # pyright: ignore
    assert len({x["commit"] for x in manifest["upload"]}) == 1  # pyright: ignore

    assert manifest == expected_manifest


@pytest.mark.parametrize(
    "support, expected_tags",
    [
        ("major", {"1"}),
        ("minor", {"1", "1.0"}),
        ("patch", {"1", "1.0", "1.0.1"}),
    ],
)
def test_oci_factory_manifest_with_support(
    support: Literal["major", "minor", "patch"], expected_tags: set[str]
):
    repository = "canonical/prometheus-rock"
    commit = "abcdef123"
    versions_with_tags = {"1.0.1": ["1", "1.0", "1.0.1"]}
    end_of_life_date = datetime.now() + timedelta(days=91)
    end_of_life = f"{end_of_life_date.strftime('%Y-%m-%d')}T00:00:00Z"

    manifest: Dict = yaml.safe_load(
        rockcraft.oci_factory_manifest(
            repository,
            commit,
            versions_with_tags,
            risk_track="stable",
            support=support,
        )
    )
    release = manifest["upload"][0]["release"]  # pyright: ignore

    # Only supported tags should be present, each with the standard EOL
    assert set(release.keys()) == expected_tags
    for tag in expected_tags:
        assert release[tag]["end-of-life"] == end_of_life


@pytest.mark.parametrize(
    "eol_date",
    [
        datetime(2027, 1, 1),
        datetime(2028, 6, 15),
        datetime(2030, 12, 31),
    ],
)
def test_oci_factory_manifest_with_custom_eol(eol_date: datetime):
    repository = "canonical/prometheus-rock"
    commit = "abcdef123"
    versions_with_tags = {"1.0.1": ["1", "1.0", "1.0.1"]}
    end_of_life = f"{eol_date.strftime('%Y-%m-%d')}T00:00:00Z"

    manifest: Dict = yaml.safe_load(
        rockcraft.oci_factory_manifest(
            repository,
            commit,
            versions_with_tags,
            risk_track="stable",
            support="minor",
            eol=eol_date,
        )
    )
    release = manifest["upload"][0]["release"]  # pyright: ignore

    # "1" and "1.0" are supported (major/minor), so they get the custom EOL
    assert release["1"]["end-of-life"] == end_of_life
    assert release["1.0"]["end-of-life"] == end_of_life
    # "1.0.1" is a patch tag, unsupported at minor level, so it's omitted entirely
    assert "1.0.1" not in release
