# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Small native RBD helpers for the experimental RBD storage domain.

This module intentionally does not use os-brick.  VM data images are accessed
by qemu through librbd using libvirt network disks.  Only the domain lock image
is mapped on the host via krbd so sanlock can use normal block-device paths.

Engine-side integration is expected to pass RBD connection parameters
through StorageDomain.create(..., typeArgs={...}) or
StoragePool.connectStorageServer().  For manual tests, the same values can be
provided through environment variables:

    VDSM_RBD_POOLS=ovirt[,otherpool]
    VDSM_RBD_MONITORS=mon1[:6789],mon2[:6789]
    VDSM_RBD_CEPH_USER=ovirt
    VDSM_RBD_SECRET_UUID=<libvirt-secret-uuid>
"""

import json
import os
import uuid

from vdsm.common import cmdutils
from vdsm.common import commands
from vdsm.common import supervdsm


DEFAULT_POOL = "ovirt"
LOCK_IMAGE_TEMPLATE = "ovirt-sd-%s-lock"
VG_TEMPLATE = "rbdlock-%s"
VOLUME_IMAGE_TEMPLATE = "volume-%s"
CREDENTIALS_DIR = "/var/lib/vdsm/rbd"
SECRET_UUID_NAMESPACE = uuid.UUID("86f7b397-8e4a-4f4e-bc22-3f2160f8e860")

_runtime_connections = {}
_runtime_domain_connections = {}


class RBDCommandError(RuntimeError):
    pass


def configured_pools():
    pools = set(_runtime_connections)
    pools.update(conn["pool"] for conn in _runtime_domain_connections.values())
    env_pools = os.environ.get("VDSM_RBD_POOLS", DEFAULT_POOL)
    pools.update(pool.strip() for pool in env_pools.split(",")
                 if pool.strip())
    return sorted(pools)


def normalize_connection(params, sd_uuid=None):
    params = dict(params or {})
    pool = (params.get("pool") or params.get("rbdPool") or
            params.get("poolName") or params.get("remotePath") or
            params.get("connection") or params.get("id") or DEFAULT_POOL)
    sd_uuid = (sd_uuid or params.get("sdUUID") or
               params.get("storageDomainID") or
               params.get("storagedomainID") or
               params.get("storageDomainUUID") or
               params.get("domainUUID") or "")
    monitors = monitors_to_string(params.get("monitors") or
                                  params.get("rbdMonitors") or
                                  params.get("hosts") or
                                  params.get("portal") or "")
    ceph_user = normalize_ceph_user(params.get("cephUser") or
                                    params.get("ceph_user") or
                                    params.get("username") or
                                    params.get("user") or "")
    ceph_key = _first(params, "cephKey", "ceph_key", "key",
                      "cephxKey", "rbdKey", "password", default="")
    secret_uuid = (params.get("secretUUID") or params.get("secretUuid") or
                   params.get("libvirtSecretUUID") or "")
    if sd_uuid and ceph_user and not secret_uuid:
        secret_uuid = domain_secret_uuid(sd_uuid, ceph_user)
    keyring_path = (params.get("keyringPath") or params.get("keyring") or "")
    if sd_uuid and ceph_user and not keyring_path:
        keyring_path = domain_keyring_path(sd_uuid, ceph_user)
    return {
        "id": params.get("id") or sd_uuid or pool,
        "pool": pool,
        "sdUUID": sd_uuid,
        "monitors": monitors,
        "cephUser": ceph_user,
        "cephKey": str(ceph_key or "").strip(),
        "secretUUID": str(secret_uuid or "").strip(),
        "keyringPath": str(keyring_path or "").strip(),
    }


def register_pool(pool, monitors=None, ceph_user=None, secret_uuid=None):
    return register_connection({
        "id": pool,
        "pool": pool,
        "monitors": monitors,
        "cephUser": ceph_user,
        "secretUUID": secret_uuid,
    })


def register_connection(params):
    conn = _public_connection(normalize_connection(params))
    _runtime_connections[conn["pool"]] = conn
    if conn.get("sdUUID"):
        _runtime_domain_connections[conn["sdUUID"]] = conn
    return conn


def unregister_connection(params):
    conn = normalize_connection(params)
    _runtime_connections.pop(conn["pool"], None)
    if conn.get("sdUUID"):
        _runtime_domain_connections.pop(conn["sdUUID"], None)
    return _public_connection(conn)


def registered_config(pool):
    conn = connection_for_pool(pool)
    return {
        "pool": conn["pool"],
        "monitors": conn.get("monitors", ""),
        "ceph_user": conn.get("cephUser", ""),
        "secret_uuid": conn.get("secretUUID", ""),
        "keyring_path": conn.get("keyringPath", ""),
    }


def connection_for_pool(pool, sd_uuid=None):
    if sd_uuid and sd_uuid in _runtime_domain_connections:
        return _runtime_domain_connections[sd_uuid]
    conn = _runtime_connections.get(pool)
    if conn:
        return conn
    ceph_user = normalize_ceph_user(os.environ.get(
        "VDSM_RBD_CEPH_USER", ""))
    secret_uuid = os.environ.get("VDSM_RBD_SECRET_UUID", "")
    keyring_path = os.environ.get("VDSM_RBD_KEYRING", "")
    return {
        "id": pool,
        "pool": pool,
        "sdUUID": sd_uuid or "",
        "monitors": monitors_to_string(os.environ.get(
            "VDSM_RBD_MONITORS", "")),
        "cephUser": ceph_user,
        "secretUUID": secret_uuid,
        "keyringPath": keyring_path,
    }


def domain_secret_uuid(sd_uuid, ceph_user):
    user = normalize_ceph_user(ceph_user) or "admin"
    return str(uuid.uuid5(SECRET_UUID_NAMESPACE,
                          "rbd-secret:%s:%s" % (sd_uuid, user)))


def domain_keyring_path(sd_uuid, ceph_user):
    user = normalize_ceph_user(ceph_user) or "admin"
    return os.path.join(
        CREDENTIALS_DIR,
        str(sd_uuid),
        "ceph.client.%s.keyring" % user,
    )


def prepare_domain_credentials(sd_uuid, pool, monitors=None, ceph_user=None,
                               ceph_key=None, secret_uuid=None):
    ceph_user = normalize_ceph_user(ceph_user)
    if not secret_uuid and sd_uuid and ceph_user:
        secret_uuid = domain_secret_uuid(sd_uuid, ceph_user)
    keyring_path = ""
    if sd_uuid and ceph_user:
        keyring_path = domain_keyring_path(sd_uuid, ceph_user)

    if ceph_key:
        result = supervdsm.getProxy().rbd_prepare_credentials(
            sd_uuid,
            ceph_user,
            str(ceph_key).strip(),
            secret_uuid,
            monitors_to_string(monitors),
        )
        secret_uuid = result.get("secretUUID", secret_uuid)
        keyring_path = result.get("keyringPath", keyring_path)

    conn = register_connection({
        "id": sd_uuid or pool,
        "pool": pool,
        "sdUUID": sd_uuid,
        "monitors": monitors,
        "cephUser": ceph_user,
        "secretUUID": secret_uuid,
        "keyringPath": keyring_path,
    })
    return conn


def remove_domain_credentials(sd_uuid, ceph_user=None, secret_uuid=None):
    return supervdsm.getProxy().rbd_remove_credentials(
        sd_uuid,
        normalize_ceph_user(ceph_user),
        secret_uuid,
    )


def monitors_to_string(monitors):
    if monitors is None:
        monitors = ""
    if isinstance(monitors, (list, tuple)):
        values = []
        for item in monitors:
            if isinstance(item, dict):
                host = item.get("name") or item.get("host") or ""
                port = item.get("port") or "6789"
                values.append(_monitor_to_string(host, port))
            else:
                values.append(str(item))
        monitors = ",".join(values)
    return ",".join(_normalize_monitor_item(item)
                    for item in str(monitors).split(",")
                    if item.strip())


def parse_monitors(monitors):
    result = []
    for item in monitors_to_string(monitors).split(","):
        if not item:
            continue
        if ":" in item:
            host, port = item.rsplit(":", 1)
        else:
            host, port = item, "6789"
        result.append({"name": host, "port": str(port)})
    return result


monitors_from_string = parse_monitors
normalize_monitors = monitors_to_string


def monitors(pool=None, sd_uuid=None):
    if pool is None:
        raw = os.environ.get("VDSM_RBD_MONITORS", "")
    else:
        raw = connection_for_pool(pool, sd_uuid).get("monitors", "")
    return parse_monitors(raw)


def normalize_ceph_user(user):
    user = str(user or "").strip()
    if user.startswith("client."):
        user = user[len("client."):]
    return user


def ceph_user_for_libvirt(pool=None, sd_uuid=None):
    if pool is None:
        user = os.environ.get("VDSM_RBD_CEPH_USER", "ovirt")
    else:
        user = connection_for_pool(pool, sd_uuid).get("cephUser") or "ovirt"
    return normalize_ceph_user(user)


def libvirt_secret_uuid(pool=None, sd_uuid=None, ceph_user=None):
    if pool is None:
        value = os.environ.get("VDSM_RBD_SECRET_UUID", "")
    else:
        value = connection_for_pool(pool, sd_uuid).get("secretUUID", "")
    if value:
        return value
    if sd_uuid:
        return domain_secret_uuid(sd_uuid, ceph_user_for_libvirt(
            pool, sd_uuid) if not ceph_user else ceph_user)
    return ""


def libvirt_auth(pool=None, ceph_user=None, secret_uuid=None, sd_uuid=None):
    user = normalize_ceph_user(ceph_user)
    if not user:
        user = ceph_user_for_libvirt(pool, sd_uuid)
    secret_uuid = secret_uuid or libvirt_secret_uuid(
        pool, sd_uuid=sd_uuid, ceph_user=user)
    if not secret_uuid:
        return None
    return {
        "username": user,
        "secretUUID": secret_uuid,
    }


def lock_image_name(sd_uuid):
    return LOCK_IMAGE_TEMPLATE % sd_uuid


def vg_name(sd_uuid):
    return VG_TEMPLATE % sd_uuid


def volume_image_name(vol_uuid):
    return VOLUME_IMAGE_TEMPLATE % vol_uuid


def rbd_spec(pool, image):
    return "%s/%s" % (pool, image)


def list_images(pool, ceph_user=None, keyring_path=None, monitors=None,
                sd_uuid=None):
    out = _run(_rbd_cmd(
        pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
        sd_uuid=sd_uuid) + ["ls", pool])
    return out.decode("utf-8").splitlines()


def image_exists(pool, image, ceph_user=None, keyring_path=None,
                 monitors=None, sd_uuid=None):
    try:
        _run(_rbd_cmd(
            pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
            sd_uuid=sd_uuid) + ["info", rbd_spec(pool, image)])
        return True
    except RBDCommandError:
        return False


def image_info(pool, image, ceph_user=None, keyring_path=None,
               monitors=None, sd_uuid=None):
    out = _run(_rbd_cmd(
        pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
        sd_uuid=sd_uuid) + [
            "info", rbd_spec(pool, image), "--format", "json"])
    return json.loads(out.decode("utf-8"))


def image_size(pool, image, ceph_user=None, keyring_path=None,
               monitors=None, sd_uuid=None):
    return int(image_info(
        pool, image, ceph_user=ceph_user, keyring_path=keyring_path,
        monitors=monitors, sd_uuid=sd_uuid)["size"])


def list_volume_images(pool, ceph_user=None, keyring_path=None,
                       monitors=None, sd_uuid=None):
    return [name for name in list_images(
        pool, ceph_user=ceph_user, keyring_path=keyring_path,
        monitors=monitors, sd_uuid=sd_uuid) if name.startswith("volume-")]


def create_image(pool, image, size_bytes, ceph_user=None, keyring_path=None,
                 monitors=None, sd_uuid=None):
    size_mib = int(size_bytes // 1024 // 1024)
    if size_mib <= 0:
        raise ValueError("RBD image size must be positive")
    _run(_rbd_cmd(
        pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
        sd_uuid=sd_uuid) + [
            "create", rbd_spec(pool, image),
            "--size", str(size_mib),
            "--image-format", "2",
        ])


def remove_image(pool, image, ceph_user=None, keyring_path=None,
                 monitors=None, sd_uuid=None):
    _run(_rbd_cmd(
        pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
        sd_uuid=sd_uuid) + ["rm", rbd_spec(pool, image), "--no-progress"])


def resize_image(pool, image, size_bytes, ceph_user=None, keyring_path=None,
                 monitors=None, sd_uuid=None):
    size_mib = int(size_bytes // 1024 // 1024)
    if size_mib <= 0:
        raise ValueError("RBD image size must be positive")
    _run(_rbd_cmd(
        pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
        sd_uuid=sd_uuid) + [
            "resize", rbd_spec(pool, image), "--size", str(size_mib)])


def set_image_meta(pool, image, values, ceph_user=None, keyring_path=None,
                   monitors=None, sd_uuid=None):
    for key, value in values.items():
        if value is None:
            value = ""
        _run(_rbd_cmd(
            pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
            sd_uuid=sd_uuid) + [
                "image-meta", "set", rbd_spec(pool, image),
                str(key), str(value),
            ])


def get_image_meta(pool, image, key, default=None, ceph_user=None,
                   keyring_path=None, monitors=None, sd_uuid=None):
    try:
        out = _run(_rbd_cmd(
            pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
            sd_uuid=sd_uuid) + [
                "image-meta", "get", rbd_spec(pool, image), key])
    except RBDCommandError:
        return default
    return out.decode("utf-8").strip()


def get_image_metadata(pool, image, ceph_user=None, keyring_path=None,
                       monitors=None, sd_uuid=None):
    try:
        out = _run(_rbd_cmd(
            pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
            sd_uuid=sd_uuid) + [
                "image-meta", "list", rbd_spec(pool, image),
                "--format", "json",
            ])
    except RBDCommandError:
        return {}
    return json.loads(out.decode("utf-8"))


def map_lock_image(pool, image, ceph_user=None, keyring_path=None,
                   monitors=None, sd_uuid=None):
    conn = connection_for_pool(pool, sd_uuid)
    user = ceph_user or conn.get("cephUser")
    keyring_path = keyring_path or conn.get("keyringPath")
    monitors = monitors or conn.get("monitors")
    return supervdsm.getProxy().rbd_map_lock_image(
        pool, image, user, monitors_to_string(monitors), keyring_path)


def unmap_lock_image(path):
    return supervdsm.getProxy().rbd_unmap_lock_image(path)


def create_domain_layout(pool, sd_uuid, lock_size_mib, lv_sizes,
                         ceph_user=None, keyring_path=None, monitors=None):
    conn = connection_for_pool(pool, sd_uuid)
    user = ceph_user or conn.get("cephUser")
    keyring_path = keyring_path or conn.get("keyringPath")
    monitors = monitors or conn.get("monitors")
    return supervdsm.getProxy().rbd_create_domain_layout(
        pool,
        sd_uuid,
        int(lock_size_mib),
        list(lv_sizes),
        user,
        monitors_to_string(monitors),
        keyring_path,
    )


def remove_domain_layout(pool, sd_uuid, ceph_user=None, keyring_path=None,
                         monitors=None):
    conn = connection_for_pool(pool, sd_uuid)
    user = ceph_user or conn.get("cephUser")
    keyring_path = keyring_path or conn.get("keyringPath")
    monitors = monitors or conn.get("monitors")
    return supervdsm.getProxy().rbd_remove_domain_layout(
        pool, sd_uuid, user, monitors_to_string(monitors), keyring_path)


def vg_uuid(name):
    return supervdsm.getProxy().rbd_vg_uuid(name)


def activate_vg(name):
    return supervdsm.getProxy().rbd_activate_vg(name)


def deactivate_vg(name):
    return supervdsm.getProxy().rbd_deactivate_vg(name)


def lv_path(vg, lv):
    # Avoid importing storage.lvm here so supervdsm and storage code can both
    # reuse this helper without triggering storage subsystem initialization.
    return "/dev/%s/%s" % (vg, lv)


def pool_stats(pool, ceph_user=None, keyring_path=None, monitors=None,
               sd_uuid=None):
    try:
        out = _run(_ceph_cmd(
            pool, ceph_user, keyring_path=keyring_path, monitors=monitors,
            sd_uuid=sd_uuid) + ["df", "--format", "json"])
        data = json.loads(out.decode("utf-8"))
    except Exception:
        return {"diskfree": 0, "disktotal": 0}

    for pool_info in data.get("pools", []):
        if pool_info.get("name") != pool:
            continue
        stats = pool_info.get("stats", {})
        free = int(stats.get("max_avail", 0))
        used = int(stats.get("bytes_used", stats.get("stored", 0)))
        return {"diskfree": free, "disktotal": free + used}
    return {"diskfree": 0, "disktotal": 0}


pool_capacity = pool_stats


def _rbd_cmd(pool=None, ceph_user=None, keyring_path=None, monitors=None,
             sd_uuid=None):
    cmd = ["rbd"]
    conn = connection_for_pool(pool, sd_uuid) if pool is not None else {}
    user = normalize_ceph_user(ceph_user or conn.get("cephUser"))
    monitors = monitors_to_string(monitors or conn.get("monitors", ""))
    keyring_path = keyring_path or conn.get("keyringPath", "")
    if monitors:
        cmd.extend(["-m", monitors])
    if user:
        cmd.extend(["--id", user])
    if keyring_path:
        cmd.extend(["--keyring", keyring_path])
    return cmd


def _ceph_cmd(pool=None, ceph_user=None, keyring_path=None, monitors=None,
              sd_uuid=None):
    cmd = ["ceph"]
    conn = connection_for_pool(pool, sd_uuid) if pool is not None else {}
    user = normalize_ceph_user(ceph_user or conn.get("cephUser"))
    monitors = monitors_to_string(monitors or conn.get("monitors", ""))
    keyring_path = keyring_path or conn.get("keyringPath", "")
    if monitors:
        cmd.extend(["-m", monitors])
    if user:
        cmd.extend(["--id", user])
    if keyring_path:
        cmd.extend(["--keyring", keyring_path])
    return cmd


def _normalize_monitor_item(item):
    item = item.strip()
    if not item:
        return ""
    if ":" in item:
        host, port = item.rsplit(":", 1)
    else:
        host, port = item, "6789"
    return _monitor_to_string(host, port)


def _monitor_to_string(host, port):
    host = str(host).strip()
    port = str(port or "6789").strip()
    if not host:
        return ""
    return "%s:%s" % (host, port)


def _first(params, *names, **kwargs):
    default = kwargs.get("default")
    for name in names:
        if name in params and params[name] not in (None, ""):
            return params[name]
    return default


def _public_connection(conn):
    public = dict(conn)
    public.pop("cephKey", None)
    return public


def _run(cmd):
    try:
        return commands.run(cmd)
    except cmdutils.Error as e:
        raise RBDCommandError("RBD command failed: %r: %s" % (cmd, e))
