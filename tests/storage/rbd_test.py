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
