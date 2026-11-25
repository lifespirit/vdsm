# SPDX-FileCopyrightText: oVirt Developers
# SPDX-License-Identifier: GPL-2.0-or-later

"""Unit tests for persistent, key-free RBD connection registry."""

import pytest

from vdsm.storage import rbd_utils


class FakeSuperVdsm:
    def __init__(self):
        self.saved = []
        self.keyrings = []

    def rbd_load_connections(self):
        return [dict(x) for x in self.saved]

    def rbd_save_connections(self, entries):
        assert all("cephKey" not in item for item in entries)
        self.saved = [dict(x) for x in entries]

    def rbd_write_ceph_keyring(self, identity, user, key):
        self.keyrings.append((identity, user, key))
        return "/var/lib/vdsm/rbd/%s.keyring" % identity


@pytest.fixture
def registry(monkeypatch):
    proxy = FakeSuperVdsm()
    monkeypatch.setattr(rbd_utils.supervdsm, "getProxy", lambda: proxy)
    monkeypatch.setattr(rbd_utils, "ensure_libvirt_secret", lambda *a: None)
    monkeypatch.setattr(rbd_utils, "_registry_loaded", False)
    monkeypatch.setattr(rbd_utils, "_saved_connections", [])
    monkeypatch.setattr(rbd_utils, "_runtime_connections", {})
    monkeypatch.setattr(rbd_utils, "_domain_connections", {})
    monkeypatch.setattr(rbd_utils, "_default_pool_connections", {})
    return proxy


def test_registration_never_persists_cephx_key(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "client.vm",
        "monitors": "mon1:3300", "cephKey": "secret-value",
    })
    assert registry.saved[0]["pool"] == "pool1"
    assert registry.saved[0]["keyring"].endswith("domain1.keyring")
    assert "cephKey" not in registry.saved[0]


def test_restore_connection_after_process_restart(registry, monkeypatch):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "monitors": "mon1:3300", "cephKey": "secret-value",
    })
    monkeypatch.setattr(rbd_utils, "_registry_loaded", False)
    monkeypatch.setattr(rbd_utils, "_saved_connections", [])
    monkeypatch.setattr(rbd_utils, "_runtime_connections", {})
    monkeypatch.setattr(rbd_utils, "_domain_connections", {})
    monkeypatch.setattr(rbd_utils, "_default_pool_connections", {})
    restored = rbd_utils.connection_for_domain("domain1")
    assert restored["pool"] == "pool1"
    assert restored["keyring"] == "/var/lib/vdsm/rbd/domain1.keyring"
    assert restored["monitors"] == "mon1:3300"
    assert "pool1" in rbd_utils.configured_pools()


def test_reconnect_does_not_drop_keyring(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "cephKey": "secret-value",
    })
    restored = rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
    })
    assert restored["keyring"].endswith("domain1.keyring")


def test_unregistration_persists(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1",
    })
    rbd_utils.unregister_connection({
        "pool": "pool1", "sdUUID": "domain1",
    })
    assert registry.saved == []


def test_ceph_command_uses_monitors_and_keyring(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "cephUser": "vm", "monitors": "mon1:3300",
        "cephKey": "secret-value",
    })
    cmd = rbd_utils._ceph_cmd("pool1", "vm")
    assert "--mon-host" in cmd
    assert "--keyring" in cmd


def test_load_failure_does_not_hide_storage_error(registry, monkeypatch):
    def fail():
        raise OSError("registry unavailable")
    monkeypatch.setattr(registry, "rbd_load_connections", fail)
    with pytest.raises(OSError, match="registry unavailable"):
        rbd_utils.configured_pools()
