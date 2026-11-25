# SPDX-FileCopyrightText: oVirt Developers
# SPDX-License-Identifier: GPL-2.0-or-later

"""Unit tests for persistent, key-free RBD connection registry."""

import pytest

from vdsm.storage import rbd_utils


class FakeSuperVdsm:
    def __init__(self):
        self.saved = []
        self.keyrings = []
        self.recovered = []

    def rbd_load_connections(self):
        return [dict(x) for x in self.saved]

    def rbd_save_connections(self, entries):
        assert all("cephKey" not in item for item in entries)
        self.saved = [dict(x) for x in entries]

    def rbd_write_ceph_keyring(self, identity, user, key):
        self.keyrings.append((identity, user, key))
        return "/var/lib/vdsm/rbd/%s.keyring" % identity

    def rbd_ensure_libvirt_secret(self, uuid, user, keyring, label):
        self.recovered.append((uuid, user, keyring, label))
        return uuid


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


def test_same_pool_same_user_domain_specific_credentials(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "monitors": "mon-a:3300", "cephKey": "secret-one",
    })
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain2", "cephUser": "vm",
        "monitors": "mon-b:3300", "cephKey": "secret-two",
    })
    cmd_a = rbd_utils._rbd_cmd("pool1", "vm", sd_uuid="domain1")
    cmd_b = rbd_utils._rbd_cmd("pool1", "vm", sd_uuid="domain2")
    assert "domain1.keyring" in " ".join(cmd_a)
    assert "domain2.keyring" in " ".join(cmd_b)
    assert "mon-a:3300" in cmd_a
    assert "mon-b:3300" in cmd_b
    assert cmd_a != cmd_b
    assert len(registry.saved) == 2


def test_ambiguous_pool_without_domain_fails_closed(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "cephKey": "secret-one",
    })
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain2", "cephUser": "vm",
        "cephKey": "secret-two",
    })
    with pytest.raises(rbd_utils.RBDCommandError, match="Ambiguous"):
        rbd_utils._rbd_cmd("pool1", "vm")


def test_domain_pool_mismatch_fails_closed(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "cephKey": "secret-value",
    })
    with pytest.raises(rbd_utils.RBDCommandError, match="mismatch"):
        rbd_utils._rbd_cmd("another-pool", "vm", sd_uuid="domain1")


def test_domain_secret_restored_from_keyring(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "cephKey": "secret-value",
    })
    auth = rbd_utils.libvirt_auth("pool1", "vm", sd_uuid="domain1")
    assert auth["username"] == "vm"
    assert auth["secretUUID"]
    assert registry.recovered == [(
        auth["secretUUID"], "vm",
        "/var/lib/vdsm/rbd/domain1.keyring", "domain1",
    )]


def test_domain_secret_requires_keyring(registry):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
    })
    with pytest.raises(rbd_utils.RBDCommandError, match="no keyring"):
        rbd_utils.libvirt_auth("pool1", "vm", sd_uuid="domain1")


def test_recovery_after_registry_reload_uses_original_domain_keyring(
        registry, monkeypatch):
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain1", "cephUser": "vm",
        "cephKey": "first",
    })
    rbd_utils.register_connection({
        "pool": "pool1", "sdUUID": "domain2", "cephUser": "vm",
        "cephKey": "second",
    })
    monkeypatch.setattr(rbd_utils, "_registry_loaded", False)
    monkeypatch.setattr(rbd_utils, "_saved_connections", [])
    monkeypatch.setattr(rbd_utils, "_runtime_connections", {})
    monkeypatch.setattr(rbd_utils, "_domain_connections", {})
    monkeypatch.setattr(rbd_utils, "_default_pool_connections", {})
    rbd_utils.libvirt_auth("pool1", "vm", sd_uuid="domain1")
    assert registry.recovered[-1][2].endswith("domain1.keyring")
