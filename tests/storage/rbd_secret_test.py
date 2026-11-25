# SPDX-FileCopyrightText: oVirt Developers
# SPDX-License-Identifier: GPL-2.0-or-later

"""Regression tests for restoring a CephX libvirt secret on the host."""

import base64
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from vdsm.supervdsm_api import rbd


class FakeSecret:
    def __init__(self):
        self.value = None

    def setValue(self, value, flags):
        self.value = value
        assert flags == 0


class FakeLibvirtConnection:
    def __init__(self):
        self.secret = FakeSecret()
        self.xml = None
        self.closed = False

    def secretDefineXML(self, xml):
        self.xml = xml
        return self.secret

    def close(self):
        self.closed = True


@pytest.fixture
def root_keyring(monkeypatch, tmp_path):
    monkeypatch.setattr(rbd, "KEYRING_DIR", str(tmp_path))
    keyring = tmp_path / "domain1.keyring"
    keyring.write_text(
        "[client.vm]\n    key = %s\n" %
        base64.b64encode(b"ceph-secret-bytes").decode("ascii"))
    os.chmod(str(keyring), 0o600)
    # Unit tests normally run as vdsm, not root; emulate root ownership
    # while retaining real file type and mode bits.
    real_fstat = rbd.os.fstat
    monkeypatch.setattr(
        rbd.os, "fstat",
        lambda fd: SimpleNamespace(
            st_uid=0, st_mode=real_fstat(fd).st_mode),
    )
    return str(keyring)


def test_restore_libvirt_secret_from_keyring(monkeypatch, root_keyring):
    conn = FakeLibvirtConnection()
    monkeypatch.setitem(
        sys.modules, "libvirt",
        SimpleNamespace(open=lambda uri: conn),
    )
    secret_uuid = "abbe3752-1b0f-4c7c-9cf6-2e16472e6f62"
    assert rbd.rbd_ensure_libvirt_secret(
        secret_uuid, "vm", root_keyring, "domain1") == secret_uuid
    assert conn.secret.value == b"ceph-secret-bytes"
    assert secret_uuid in conn.xml
    assert "client.vm" in conn.xml
    assert conn.closed


def test_reject_mismatched_keyring_user(root_keyring):
    with pytest.raises(ValueError, match="identity mismatch"):
        rbd._read_keyring("not-vm", root_keyring)


def test_reject_keyring_outside_configured_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(rbd, "KEYRING_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="Invalid Ceph keyring path"):
        rbd._read_keyring("vm", "/tmp/untrusted.keyring")


def test_reject_symbolic_link_keyring(monkeypatch, root_keyring, tmp_path):
    link = tmp_path / "linked.keyring"
    link.symlink_to(root_keyring)
    with pytest.raises(OSError):
        rbd._read_keyring("vm", str(link))
