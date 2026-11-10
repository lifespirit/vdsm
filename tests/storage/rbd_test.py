# SPDX-FileCopyrightText: oVirt Developers
# SPDX-License-Identifier: GPL-2.0-or-later

"""Regression tests for native RBD storage domain P0 fixes."""

from types import SimpleNamespace

from vdsm.storage import constants as sc
from vdsm.storage import rbdSD
from vdsm.storage import rbdVolume
from vdsm.storage import sd


DOMAIN_A = "domain-a"
DOMAIN_B = "domain-b"
POOL = "shared-pool"
IMAGE_A = "image-a"


def _manifest():
    manifest = object.__new__(rbdSD.RBDStorageDomainManifest)
    manifest.sdUUID = DOMAIN_A
    manifest.pool = POOL
    manifest._ceph_user = "ovirt"
    return manifest


def _install_volume_metadata(monkeypatch):
    volume_metadata = {
        "volume-vol-a": {
            sc.DOMAIN: DOMAIN_A,
            sc.IMAGE: IMAGE_A,
            sc.PUUID: sd.BLANK_UUID,
        },
        "volume-vol-b": {
            sc.DOMAIN: DOMAIN_B,
            sc.IMAGE: "image-b",
            sc.PUUID: sd.BLANK_UUID,
        },
        "volume-vol-c": {
            sc.DOMAIN: DOMAIN_A,
            sc.IMAGE: "image-c",
            sc.PUUID: "parent-c",
        },
        "volume-vol-incomplete": {
            sc.DOMAIN: DOMAIN_A,
        },
    }
    monkeypatch.setattr(
        rbdSD.rbd_utils,
        "list_volume_images",
        lambda *args, **kwargs: list(volume_metadata),
    )
    monkeypatch.setattr(
        rbdSD.rbd_utils,
        "get_image_metadata",
        lambda pool, name, **kwargs: volume_metadata[name],
    )


def test_stats_use_numeric_ceph_pool_values(monkeypatch):
    stats = {"diskfree": 1200, "disktotal": 3000}
    monkeypatch.setattr(
        rbdSD.rbd_utils,
        "pool_capacity",
        lambda *args, **kwargs: stats,
    )
    dom = object.__new__(rbdSD.RBDStorageDomain)
    dom._manifest = SimpleNamespace(
        pool=POOL, ceph_user=lambda: "ovirt")
    result = dom.getStats()

    assert result["diskfree"] == 1200
    assert result["disktotal"] == 3000
    assert isinstance(result["diskfree"], int)


def test_discovery_only_returns_owned_images(monkeypatch):
    _install_volume_metadata(monkeypatch)
    assert set(_manifest().getAllImages()) == {IMAGE_A, "image-c"}


def test_discovery_only_returns_owned_volumes(monkeypatch):
    _install_volume_metadata(monkeypatch)
    volumes = _manifest().getAllVolumes()

    assert set(volumes) == {"vol-a", "vol-c"}
    assert volumes["vol-c"].parent == "parent-c"


def test_get_image_volumes_filters_other_domains(monkeypatch):
    _install_volume_metadata(monkeypatch)
    monkeypatch.setattr(
        rbdVolume.sdCache,
        "produce_manifest",
        lambda sd_uuid: SimpleNamespace(
            pool=POOL, ceph_user=lambda: "ovirt"),
    )
    assert rbdVolume.RBDVolume.getImageVolumes(DOMAIN_A, IMAGE_A) == [
        "vol-a"
    ]


def test_volume_manifest_can_validate_during_init(monkeypatch):
    monkeypatch.setattr(
        rbdVolume, "_pool_for_domain", lambda sd_uuid: POOL)
    monkeypatch.setattr(
        rbdVolume.sdCache,
        "produce_manifest",
        lambda sd_uuid: SimpleNamespace(
            ceph_user=lambda: "ovirt"),
    )
    monkeypatch.setattr(
        rbdVolume.rbd_utils,
        "image_exists",
        lambda *args, **kwargs: True,
    )
    vol = rbdVolume.RBDVolumeManifest(
        "/repo", DOMAIN_A, IMAGE_A, "vol-a")
    assert vol.pool == POOL
    assert vol.getVolumePath() == POOL + "/volume-vol-a"



def test_missing_image_is_not_a_ceph_connection_error(monkeypatch):
    monkeypatch.setattr(
        rbdSD.rbd_utils, "list_images",
        lambda *args, **kwargs: ["volume-existing"])
    assert rbdSD.rbd_utils.image_exists(POOL, "volume-missing") is False
    assert rbdSD.rbd_utils.image_exists(POOL, "volume-existing") is True


def test_image_exists_propagates_ceph_errors(monkeypatch):
    import pytest

    def unavailable(*args, **kwargs):
        raise rbdSD.rbd_utils.RBDCommandError("Ceph unavailable")

    monkeypatch.setattr(rbdSD.rbd_utils, "list_images", unavailable)
    with pytest.raises(rbdSD.rbd_utils.RBDCommandError):
        rbdSD.rbd_utils.image_exists(POOL, "volume-foo")


def test_create_rolls_back_image_and_lease_after_metadata_failure(monkeypatch):
    import pytest

    calls = []
    dom = SimpleNamespace(
        sdUUID=DOMAIN_A,
        manifest=SimpleNamespace(pool=POOL, ceph_user=lambda: "ovirt"),
        create_volume_lease=lambda vol: calls.append(("lease-create", vol)),
        delete_volume_lease=lambda vol: calls.append(("lease-delete", vol)),
        _getRepoPath=lambda: "/repo",
    )
    monkeypatch.setattr(
        rbdVolume.rbd_utils, "create_image",
        lambda *args, **kwargs: calls.append(("image-create", args[1])))
    monkeypatch.setattr(
        rbdVolume.rbd_utils, "remove_image",
        lambda *args, **kwargs: calls.append(("image-delete", args[1])))

    def fail_meta(*args, **kwargs):
        raise RuntimeError("metadata failed")

    monkeypatch.setattr(rbdVolume.rbd_utils, "set_image_meta", fail_meta)
    with pytest.raises(RuntimeError, match="metadata failed"):
        rbdVolume.RBDVolume.create(
            dom, IMAGE_A, 1048576, sc.RAW_FORMAT, sc.SPARSE_VOL,
            sc.DATA_DISKTYPE, "vol-a", "", sd.BLANK_UUID, sd.BLANK_UUID)

    assert calls == [
        ("image-create", "volume-vol-a"),
        ("lease-create", "vol-a"),
        ("image-delete", "volume-vol-a"),
        ("lease-delete", "vol-a"),
    ]


def test_create_retains_lease_when_image_cleanup_fails(monkeypatch):
    import pytest

    calls = []
    dom = SimpleNamespace(
        sdUUID=DOMAIN_A,
        manifest=SimpleNamespace(pool=POOL, ceph_user=lambda: "ovirt"),
        create_volume_lease=lambda vol: calls.append("lease-create"),
        delete_volume_lease=lambda vol: calls.append("lease-delete"),
        _getRepoPath=lambda: "/repo",
    )
    monkeypatch.setattr(
        rbdVolume.rbd_utils, "create_image",
        lambda *args, **kwargs: None)

    def fail_remove(*args, **kwargs):
        raise RuntimeError("RBD still exists")

    def fail_meta(*args, **kwargs):
        raise RuntimeError("metadata failed")

    monkeypatch.setattr(rbdVolume.rbd_utils, "remove_image", fail_remove)
    monkeypatch.setattr(rbdVolume.rbd_utils, "set_image_meta", fail_meta)
    with pytest.raises(RuntimeError, match="metadata failed"):
        rbdVolume.RBDVolume.create(
            dom, IMAGE_A, 1048576, sc.RAW_FORMAT, sc.SPARSE_VOL,
            sc.DATA_DISKTYPE, "vol-a", "", sd.BLANK_UUID, sd.BLANK_UUID)

    assert calls == ["lease-create"]


def test_delete_preserves_lease_if_image_removal_fails(monkeypatch):
    import pytest

    calls = []
    monkeypatch.setattr(
        rbdVolume.sdCache, "produce",
        lambda sd_uuid: SimpleNamespace(
            manifest=SimpleNamespace(ceph_user=lambda: "ovirt"),
            delete_volume_lease=lambda vol: calls.append("lease-delete"),
        ))

    def fail_remove(*args, **kwargs):
        calls.append("image-delete")
        raise RuntimeError("image busy")

    monkeypatch.setattr(rbdVolume.rbd_utils, "remove_image", fail_remove)
    vol = object.__new__(rbdVolume.RBDVolume)
    vol._manifest = SimpleNamespace(
        sdUUID=DOMAIN_A, volUUID="vol-a",
        pool=POOL, rbd_image="volume-vol-a")
    with pytest.raises(RuntimeError, match="image busy"):
        vol.delete()

    assert calls == ["image-delete"]
