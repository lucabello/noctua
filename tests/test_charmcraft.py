import json
from typing import List
from unittest.mock import MagicMock, call, mock_open, patch

import pytest
import yaml

import services.charmcraft as charmcraft
import tests.constants as constants


@pytest.mark.parametrize("metadata_file", ["metadata.yaml", "charmcraft.yaml", None])
def test_metadata(metadata_file):
    if metadata_file:
        # Patch os.path.exists to return True only for metadata_file
        with patch(
            "os.path.exists",
            MagicMock(side_effect=lambda x: True if x == metadata_file else False),
        ):
            with patch(
                "builtins.open",
                new_callable=mock_open,
                read_data=yaml.dump(constants.CHARM_METADATA_BLACKBOX),
            ):
                assert charmcraft.metadata() == constants.CHARM_METADATA_BLACKBOX
    else:
        with patch("os.path.exists", MagicMock(return_value=False)):
            with pytest.raises(charmcraft.InputError):
                charmcraft.metadata()


@pytest.mark.parametrize(
    "charm",
    ["blackbox-exporter-k8s"],
)
def test_release_status(charm: str):
    with patch("sh.charmcraft", MagicMock()) as charmcraft_mock:
        charmcraft_mock.status.return_value = constants.CHARMCRAFT_STATUS[charm]
        release_status = charmcraft.release_status("fake-charm")
        assert release_status == constants.RELEASE_STATUS[charm]


@pytest.mark.parametrize(
    "charm_name, path",
    [
        ("some-charm", "./some.charm"),
        ("some-charm", "./some.charm"),
        ("some-charm", ""),
        ("some-charm", ""),
    ],
)
@patch("rich.console.Console.print", MagicMock())
def test_upload(charm_name, path):
    with patch("os.path.exists", MagicMock(side_effect=lambda _: True if path else False)):
        with patch(
            "services.charmcraft.metadata",
            MagicMock(return_value=constants.CHARM_METADATA_BLACKBOX),
        ):
            with patch(
                "sh.charmcraft", MagicMock(return_value='{"revision": -1}'), create=True
            ) as charmcraft_mock:
                charmcraft_mock.upload.return_value = '{"revision": -2}'
                # If the charm file doesn't exist, raise an exception
                if not path:
                    with pytest.raises(charmcraft.InputError):
                        charmcraft.upload(charm_name=charm_name, path=path, dry_run=False)
                    # Verify charmcraft was not called
                    charmcraft_mock.assert_not_called()
                    charmcraft_mock.upload.assert_not_called()
                    return

                # If there is a charm file
                charmcraft.upload(charm_name=charm_name, path=path, dry_run=False)
                # Make sure charmcraft upload-resource and upload are called correctly
                charmcraft_mock.assert_called_once_with(
                    "upload-resource",
                    "some-charm",
                    "blackbox-exporter-image",
                    image="docker://quay.io/prometheus/blackbox-exporter:v0.24.0",
                    format="json",
                    _tty_out=False,
                )
                charmcraft_mock.upload.assert_called_once_with(path, format="json", _tty_out=False)


@pytest.mark.parametrize("resources", [(["resA:1", "resB:2"]), (["resA:1", "resB:2"])])
@patch("rich.console.Console.print", MagicMock())
def test_release(resources: List[str]):
    with patch("sh.charmcraft", create=True) as charmcraft_mock:
        charmcraft_mock.release = MagicMock()
        charm = "some-charm"
        channel = "track/channel"
        revision = -1
        charmcraft.release(
            charm=charm, channel=channel, revision=revision, resources=resources, dry_run=False
        )
        resources_args = [f"--resource={r}" for r in resources]
        charmcraft_mock.release.assert_called_once_with(
            charm,
            *resources_args,
            channel=channel,
            revision=revision,
            _tty_out=False,
        )


@patch("rich.console.Console.print", MagicMock())
def test_promote():
    charm = "blackbox-exporter-k8s"
    source = "latest/edge"
    target = "latest/beta"
    with patch(
        "services.charmcraft.release_status",
        MagicMock(return_value=constants.RELEASE_STATUS[charm]),
    ):
        with patch("sh.charmcraft", create=True) as charmcraft_mock:
            charmcraft_mock.release = MagicMock()
            charmcraft.promote(charm=charm, source=source, target=target)
            charmcraft_mock.release.assert_has_calls(
                [
                    call(
                        charm, "--resource=blackbox-exporter-image:1", channel=target, revision=1
                    ),
                    call(
                        charm, "--resource=blackbox-exporter-image:4", channel=target, revision=17
                    ),
                ]
            )
            # Check promotion on a closed channels is skipped
            charmcraft.promote(charm=charm, source="latest/candidate", target="latest/stable")
            # Calls should be 3 instead of 4 because "latest/stable (20.04/amd64)" is closed
            assert charmcraft_mock.release.call_count == 3


@patch("rich.console.Console.print", MagicMock())
@patch("os.path.exists", MagicMock(return_value=True))
@patch("services.charmcraft.metadata", MagicMock(return_value=constants.CHARM_METADATA_BLACKBOX))
@patch(
    "services.charmcraft.status",
    MagicMock(return_value=json.loads(constants.CHARMCRAFT_STATUS["blackbox-exporter-k8s"])),
)
def test_dry_runs():
    with patch("sh.charmcraft", MagicMock(), create=True) as charmcraft_mock:
        # upload()
        charmcraft_mock.upload = MagicMock()
        charmcraft.upload(charm_name="some-charm", path="./some.charm", dry_run=True)
        charmcraft_mock.assert_not_called()
        charmcraft_mock.upload.assert_not_called()
        # release()
        charmcraft_mock.release = MagicMock()
        charmcraft.release(
            charm="some-charm", channel="some/channel", revision=-1, resources=[], dry_run=True
        )
        charmcraft_mock.assert_not_called()
        charmcraft_mock.release.assert_not_called()
        # promote()
        charmcraft.promote(
            charm="some-charm", source="latest/edge", target="latest/beta", dry_run=True
        )


@patch("os.path.exists", MagicMock(return_value=True))
@patch(
    "services.charmcraft.CharmLibrary.from_file",
    MagicMock(return_value=constants.CHARMCRAFT_LIST_LIB_EXPECTED["catalogue-k8s"]),
)
def test_local_charm_libraries():
    with patch("os.walk") as walk_mock:
        walk_mock.return_value = [
            ("lib/charms", ("catalogue_k8s", "prometheus_k8s"), ()),
            ("lib/charms/catalogue_k8s", ("v0",), ()),
            ("lib/charms/catalogue_k8s/v0", (), ("catalogue.py", "cache.pyc")),
        ]
        with patch("services.charmcraft.CharmLibrary.from_file", MagicMock()) as from_file_mock:
            from_file_mock.return_value.full_name = "charms.catalogue_k8s.v0.catalogue"
            libraries = charmcraft.local_charm_libraries()
            expected = {"charms.catalogue_k8s.v0.catalogue"}
            assert set(libraries.keys()) == expected


@patch("os.path.exists", MagicMock(return_value=False))
def test_local_charm_libraries_failures():
    with pytest.raises(charmcraft.InputError):
        charmcraft.local_charm_libraries()


@patch("rich.progress.Progress", MagicMock())
def test_outdated_charm_libraries():
    with patch(
        "services.charmcraft.CharmLibrary.from_charmhub_with_name", MagicMock()
    ) as charmhub_library_mock:
        full_name_v0 = "charms.catalogue_k8s.v0.catalogue"
        full_name_v1000 = "charms.catalogue_k8s.v1000.catalogue"
        with patch(
            "services.charmcraft.local_charm_libraries",
            MagicMock(
                return_value={
                    full_name_v0: charmcraft.CharmLibrary(
                        charm_name="catalogue-k8s", library_name="catalogue", api=0, patch=10
                    )
                }
            ),
        ):
            charmhub_library_mock.return_value = charmcraft.CharmLibrary(
                charm_name="catalogue-k8s", library_name="catalogue", api=1000, patch=1000
            )
            assert full_name_v1000 in charmcraft.outdated_charm_libraries(minor=True, major=True)
            assert full_name_v1000 in charmcraft.outdated_charm_libraries(minor=False, major=True)
            assert full_name_v1000 not in charmcraft.outdated_charm_libraries(
                minor=True, major=False
            )
            assert full_name_v1000 not in charmcraft.outdated_charm_libraries(
                minor=False, major=False
            )

            charmhub_library_mock.return_value = charmcraft.CharmLibrary(
                charm_name="catalogue-k8s", library_name="catalogue", api=0, patch=1000
            )
            assert full_name_v0 in charmcraft.outdated_charm_libraries(minor=True, major=True)
            assert full_name_v0 not in charmcraft.outdated_charm_libraries(minor=False, major=True)
            assert full_name_v0 in charmcraft.outdated_charm_libraries(minor=True, major=False)
            assert full_name_v0 not in charmcraft.outdated_charm_libraries(
                minor=False, major=False
            )


@patch("rich.console.Console.print", MagicMock())
@patch("services.charmcraft.metadata", MagicMock(return_value=constants.CHARM_METADATA_BLACKBOX))
def test_publish_charm_libraries():
    with patch(
        "services.charmcraft.local_charm_libraries",
        MagicMock(),
    ) as locals_mock:
        with patch("sh.charmcraft", MagicMock(), create=True) as charmcraft_mock:
            charmcraft_mock.return_value = '{"error_message": null}'
            locals_mock.return_value = {
                "charms.catalogue_k8s.v0.catalogue": charmcraft.CharmLibrary(
                    charm_name="catalogue-k8s", library_name="catalogue", api=0, patch=10
                )
            }
            charmcraft.publish_charm_libraries(dry_run=False)
            # Charm is blackbox, libraries are catalogue: check no calls are made
            charmcraft_mock.assert_not_called()
            # Charm is blackbox
            locals_mock.return_value = {
                "charms.blackbox_exporter_k8s.v0.blackbox_probes": charmcraft.CharmLibrary(
                    charm_name="blackbox-exporter-k8s",
                    library_name="blackbox_probes",
                    api=0,
                    patch=1,
                )
            }
            # Dry run
            charmcraft.publish_charm_libraries(dry_run=True)
            charmcraft_mock.assert_not_called()
            # Actual run
            charmcraft.publish_charm_libraries(dry_run=False)
            charmcraft_mock.assert_has_calls(
                [
                    call(
                        "publish-lib",
                        "charms.blackbox_exporter_k8s.v0.blackbox_probes",
                        format="json",
                        _tty_out=False,
                    )
                ]
            )
            # Error messages
            charmcraft_mock.return_value = '{"error_message": "is already updated"}'
            charmcraft.publish_charm_libraries(dry_run=False)
            assert charmcraft_mock.call_count == 2
            for message in [
                "is the same than in Charmhub but content is different",
                "LIBPATCH number was incorrectly incremented",
                "has a wrong LIBPATCH number, it's too high",
            ]:
                charmcraft_mock.return_value = '{"error_message": "' + message + '"}'
                with pytest.raises(charmcraft.CharmhubError):
                    charmcraft.publish_charm_libraries()


@patch("tempfile.TemporaryDirectory")
@patch("os.unlink", MagicMock())
@patch("sh.charmcraft", MagicMock(), create=True)
def test_charmcraft_pack(tempfile_mock):
    tempfile_mock.return_value.__enter__.return_value = ""
    # charmcraft.yaml needs to be backed up
    with patch("sh.cp", MagicMock(), create=True) as cp_mock:
        with patch("os.path.exists", MagicMock(return_value=True)):
            charmcraft.pack("charmcraft.yaml")
            # assert cp_mock.call_count == 2 # backup and restore
            calls = [
                call("charmcraft.yaml", "charmcraft.yaml.bak"),
                call("charmcraft.yaml.bak", "charmcraft.yaml"),
            ]
            cp_mock.assert_has_calls(calls)
            charmcraft.pack("charmcraft-22.04.yaml")
            calls.append(call("charmcraft.yaml", "charmcraft.yaml.bak"))
            calls.append(call("charmcraft-22.04.yaml", "charmcraft.yaml"))
            calls.append(call("charmcraft.yaml.bak", "charmcraft.yaml"))
            cp_mock.assert_has_calls(calls)
        with patch("os.path.exists", MagicMock(return_value=False)):
            charmcraft.pack("charmcraft-22.04.yaml")
            calls.append(call("charmcraft-22.04.yaml", "charmcraft.yaml"))
            cp_mock.assert_has_calls(calls)
