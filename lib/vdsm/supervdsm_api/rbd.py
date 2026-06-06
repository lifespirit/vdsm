# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Root-only helpers for the experimental native RBD storage domain."""

import os
import re
import subprocess

from vdsm.supervdsm_api import expose


KEYRING_DIR = "/var/lib/vdsm/rbd"


@expose
def rbd_write_ceph_keyring(identity, ceph_user, ceph_key):
    """Write a private host-local Ceph keyring and return its path."""
    identity = _safe_identity(identity)
    user = _normalize_ceph_user(ceph_user) or "ovirt"
    path = os.path.join(KEYRING_DIR, "%s.keyring" % identity)
    content = "[client.%s]\n    key = %s\n" % (user, str(ceph_key).strip())

    old_umask = os.umask(0o077)
    try:
        os.makedirs(KEYRING_DIR, mode=0o700, exist_ok=True)
        tmp = "%s.tmp" % path
        with open(tmp, "w") as f:
            f.write(content)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)
    return path


@expose
def rbd_map_lock_image(pool, image, ceph_user=None, keyring=None):
    """Map the shared lock image and return the stable krbd path."""
    spec = "%s/%s" % (pool, image)
    path = "/dev/rbd/%s/%s" % (pool, image)

    if not os.path.exists(path):
        _run(_rbd_cmd(ceph_user, keyring) + ["device", "map", spec])

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
                             ceph_user=None, keyring=None):
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
    rbd_cmd = _rbd_cmd(ceph_user, keyring)

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
            pool, sd_uuid, ceph_user=ceph_user, keyring=keyring)
        raise


@expose
def rbd_remove_domain_layout(pool, sd_uuid, ceph_user=None, keyring=None):
    _best_effort_remove_domain_layout(
        pool, sd_uuid, ceph_user=ceph_user, keyring=keyring)


@expose
def rbd_vg_uuid(vg_name):
    out = subprocess.check_output([
        "vgs", "--noheadings", "-o", "vg_uuid", vg_name])
    return out.decode("utf-8").strip()


def _best_effort_remove_domain_layout(pool, sd_uuid, ceph_user=None,
                                      keyring=None):
    lock_image = _lock_image_name(sd_uuid)
    vg_name = _vg_name(sd_uuid)
    path = _rbd_path(pool, lock_image)

    _try(["vgchange", "-an", vg_name])
    _try(["vgremove", "-ff", "-y", vg_name])
    if os.path.exists(path):
        _try(["pvremove", "-ff", "-y", path])
        _try(_rbd_cmd(ceph_user, keyring) + ["device", "unmap", path])
    _try(_rbd_cmd(ceph_user, keyring) + [
        "rm", "%s/%s" % (pool, lock_image), "--no-progress"])


def _lock_image_name(sd_uuid):
    return "ovirt-sd-%s-lock" % sd_uuid


def _vg_name(sd_uuid):
    return "rbdlock-%s" % sd_uuid


def _rbd_path(pool, image):
    return "/dev/rbd/%s/%s" % (pool, image)


def _rbd_cmd(ceph_user=None, keyring=None):
    cmd = ["rbd"]
    user = _normalize_ceph_user(ceph_user)
    if user:
        cmd.extend(["--id", user])
    if keyring:
        cmd.extend(["--keyring", str(keyring)])
    return cmd


def _normalize_ceph_user(user):
    user = str(user or "").strip()
    if user.startswith("client."):
        user = user[len("client."):]
    return user


def _safe_identity(identity):
    value = str(identity or "default")
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _try(cmd):
    try:
        _run(cmd)
    except Exception:
        pass


def _run(cmd):
    subprocess.check_call(cmd)

