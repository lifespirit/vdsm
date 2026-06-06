# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Root-only helpers for the experimental native RBD storage domain."""

import base64
import os
import shutil
import subprocess
import uuid

import libvirt

from vdsm.supervdsm_api import expose


CREDENTIALS_DIR = "/var/lib/vdsm/rbd"
SECRET_UUID_NAMESPACE = uuid.UUID("86f7b397-8e4a-4f4e-bc22-3f2160f8e860")


@expose
def rbd_prepare_credentials(sd_uuid, ceph_user, ceph_key, secret_uuid,
                            monitors=None):
    """Install per-domain Ceph credentials on this host.

    The key is used in two places:

    * a root-only Ceph keyring for VDSM-owned rbd/ceph CLI calls;
    * a persistent libvirt secret used by QEMU librbd disks.

    The key itself is never written to domain metadata and is not passed to
    subprocess command lines.
    """
    user = _normalize_ceph_user(ceph_user)
    if not user:
        raise RuntimeError("RBD ceph user is required")
    if not ceph_key:
        raise RuntimeError("RBD ceph key is required")
    if not secret_uuid:
        secret_uuid = _domain_secret_uuid(sd_uuid, user)

    keyring_path = _write_keyring(sd_uuid, user, ceph_key)
    _define_libvirt_secret(sd_uuid, user, ceph_key, secret_uuid)

    return {
        "secretUUID": secret_uuid,
        "keyringPath": keyring_path,
    }


@expose
def rbd_remove_credentials(sd_uuid, ceph_user=None, secret_uuid=None):
    user = _normalize_ceph_user(ceph_user)
    if secret_uuid:
        _undefine_libvirt_secret(secret_uuid)
    if user:
        keyring_path = _keyring_path(sd_uuid, user)
        if os.path.exists(keyring_path):
            os.unlink(keyring_path)
    root = os.path.join(CREDENTIALS_DIR, str(sd_uuid))
    if os.path.isdir(root):
        try:
            os.rmdir(root)
        except OSError:
            pass
    return True


@expose
def rbd_map_lock_image(pool, image, ceph_user=None, monitors=None,
                       keyring_path=None):
    """Map the shared lock image and return the stable krbd path."""
    spec = "%s/%s" % (pool, image)
    path = "/dev/rbd/%s/%s" % (pool, image)

    if not os.path.exists(path):
        _run(_rbd_cmd(ceph_user, monitors, keyring_path) + [
            "device", "map", spec])

    if not os.path.exists(path):
        raise RuntimeError("RBD lock image mapped but path missing: %s" % path)

    return path


@expose
def rbd_unmap_lock_image(path):
    if os.path.exists(path):
        _run(["rbd", "device", "unmap", path])


@expose
def rbd_activate_vg(vg_name):
    _run(["vgchange", "-ay", vg_name])


@expose
def rbd_deactivate_vg(vg_name):
    _run(["vgchange", "-an", vg_name])


@expose
def rbd_create_domain_layout(pool, sd_uuid, lock_size_mib, lv_sizes,
                             ceph_user=None, monitors=None,
                             keyring_path=None):
    """Create the root-owned RBD lock-image/LVM domain layout.

    This is the privileged half of rbdSD.RBDStorageDomain.create().  It
    creates only the shared lock/metadata image and the special LVs used by
    sanlock/VDSM.  VM data disks remain native RBD images and are not part of
    this VG.
    """
    lock_image = _lock_image_name(sd_uuid)
    vg_name = _vg_name(sd_uuid)
    spec = "%s/%s" % (pool, lock_image)
    path = _rbd_path(pool, lock_image)

    rbd_cmd = _rbd_cmd(ceph_user, monitors, keyring_path)
    _run(rbd_cmd + [
        "create", spec,
        "--size", str(int(lock_size_mib)),
        "--image-format", "2",
        "--image-feature", "layering",
    ])

    try:
        if not os.path.exists(path):
            _run(rbd_cmd + ["device", "map", spec])
        if not os.path.exists(path):
            raise RuntimeError(
                "RBD lock image mapped but path missing: %s" % path)

        _run(["pvcreate", "-ff", "-y", path])
        _run(["vgcreate", vg_name, path])

        for lv_name, size_mib in lv_sizes:
            _run([
                "lvcreate",
                "-n", str(lv_name),
                "-L", "%dM" % int(size_mib),
                vg_name,
            ])

        _run(["vgchange", "-ay", vg_name])
        return path
    except Exception:
        _best_effort_remove_domain_layout(
            pool, sd_uuid, ceph_user, monitors, keyring_path)
        raise


@expose
def rbd_remove_domain_layout(pool, sd_uuid, ceph_user=None, monitors=None,
                             keyring_path=None):
    _best_effort_remove_domain_layout(
        pool, sd_uuid, ceph_user, monitors, keyring_path)


@expose
def rbd_vg_uuid(vg_name):
    out = subprocess.check_output([
        "vgs", "--noheadings", "-o", "vg_uuid", vg_name])
    return out.decode("utf-8").strip()


def _best_effort_remove_domain_layout(pool, sd_uuid, ceph_user=None,
                                      monitors=None, keyring_path=None):
    lock_image = _lock_image_name(sd_uuid)
    vg_name = _vg_name(sd_uuid)
    path = _rbd_path(pool, lock_image)
    rbd_cmd = _rbd_cmd(ceph_user, monitors, keyring_path)

    _try(["vgchange", "-an", vg_name])
    _try(["vgremove", "-ff", "-y", vg_name])
    if os.path.exists(path):
        _try(["pvremove", "-ff", "-y", path])
        _try(["rbd", "device", "unmap", path])
    _try(rbd_cmd + ["rm", "%s/%s" % (pool, lock_image), "--no-progress"])


def _define_libvirt_secret(sd_uuid, ceph_user, ceph_key, secret_uuid):
    key = _decode_ceph_key(ceph_key)
    usage_name = "ovirt-rbd-%s-%s" % (sd_uuid, ceph_user)
    xml = """<secret ephemeral='no' private='yes'>
  <uuid>{uuid}</uuid>
  <usage type='ceph'>
    <name>{usage}</name>
  </usage>
</secret>
""".format(uuid=secret_uuid, usage=usage_name)

    conn = libvirt.open(None)
    if conn is None:
        raise RuntimeError("Cannot open libvirt connection")
    try:
        try:
            secret = conn.secretLookupByUUIDString(secret_uuid)
        except libvirt.libvirtError:
            secret = conn.secretDefineXML(xml, 0)
        secret.setValue(key, 0)
    finally:
        conn.close()


def _undefine_libvirt_secret(secret_uuid):
    conn = libvirt.open(None)
    if conn is None:
        return
    try:
        try:
            conn.secretLookupByUUIDString(secret_uuid).undefine()
        except libvirt.libvirtError:
            pass
    finally:
        conn.close()


def _write_keyring(sd_uuid, ceph_user, ceph_key):
    root = os.path.join(CREDENTIALS_DIR, str(sd_uuid))
    _safe_makedirs(root, 0o700)
    path = _keyring_path(sd_uuid, ceph_user)
    tmp_path = "%s.tmp" % path
    name = "client.%s" % ceph_user
    data = "[%s]\n    key = %s\n" % (name, str(ceph_key).strip())
    with open(tmp_path, "w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, 0o600)
    shutil.move(tmp_path, path)
    return path


def _keyring_path(sd_uuid, ceph_user):
    return os.path.join(
        CREDENTIALS_DIR,
        str(sd_uuid),
        "ceph.client.%s.keyring" % ceph_user,
    )


def _safe_makedirs(path, mode):
    if not os.path.isdir(path):
        os.makedirs(path, mode)
    os.chmod(path, mode)


def _decode_ceph_key(ceph_key):
    value = str(ceph_key).strip().encode("ascii")
    try:
        return base64.b64decode(value, validate=True)
    except Exception:
        # Keep a permissive fallback for tests and manually-created lab keys.
        return value


def _domain_secret_uuid(sd_uuid, ceph_user):
    return str(uuid.uuid5(SECRET_UUID_NAMESPACE,
                          "rbd-secret:%s:%s" % (sd_uuid, ceph_user)))


def _lock_image_name(sd_uuid):
    return "ovirt-sd-%s-lock" % sd_uuid


def _vg_name(sd_uuid):
    return "rbdlock-%s" % sd_uuid


def _rbd_path(pool, image):
    return "/dev/rbd/%s/%s" % (pool, image)


def _rbd_cmd(ceph_user=None, monitors=None, keyring_path=None):
    cmd = ["rbd"]
    monitors = str(monitors or "").strip()
    user = _normalize_ceph_user(ceph_user)
    if monitors:
        cmd.extend(["-m", monitors])
    if user:
        cmd.extend(["--id", user])
    if keyring_path:
        cmd.extend(["--keyring", keyring_path])
    return cmd


def _normalize_ceph_user(user):
    user = str(user or "").strip()
    if user.startswith("client."):
        user = user[len("client."):]
    return user


def _try(cmd):
    try:
        _run(cmd)
    except Exception:
        pass


def _run(cmd):
    subprocess.check_call(cmd)
