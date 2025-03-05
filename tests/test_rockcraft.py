from datetime import datetime, timedelta
from typing import Dict, List
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
    end_of_life_date = datetime.now() + timedelta(days=365 / 4)
    end_of_life = f"{end_of_life_date.strftime('%Y-%m-%d')}T00:00:00Z"

    expected_manifest = {
        "version": 1,
        "upload": [
            {
                "source": "canonical/prometheus-rock",
                "commit": "abcdef123",
                "directory": "1.0.0",
                "release": {"1.0.0": {"end-of-life": end_of_life, "risks": ["stable"]}},
            },
            {
                "source": "canonical/prometheus-rock",
                "commit": "abcdef123",
                "directory": "1.0.1",
                "release": {
                    "1": {"end-of-life": end_of_life, "risks": ["stable"]},
                    "1.0": {"end-of-life": end_of_life, "risks": ["stable"]},
                    "1.0.1": {"end-of-life": end_of_life, "risks": ["stable"]},
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
