# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Small native RBD helpers for the experimental RBD storage domain.

This module intentionally does not use os-brick.  VM data images are accessed
by qemu through librbd using libvirt network disks.  Only the domain lock image
is mapped on the host via krbd so sanlock can use normal block-device paths.

Engine-side integration should pass RBD connection parameters through
StorageDomain.create(..., typeArgs={...}) or
StoragePool.connectStorageServer().
The preferred secret flow is:

    cephUser=client.ovirt
    cephKey=<CephX key from `ceph auth get-key client.ovirt`>

VDSM creates a per-domain libvirt secret and a private host-local keyring used
by rbd/krbd commands.  Administrators do not need to pre-create
libvirt secrets.
For manual tests the same values can be provided through environment variables.
"""

import base64
import binascii
import json
import os
import uuid
from xml.sax.saxutils import escape

from vdsm.common import cmdutils
from vdsm.common import commands
from vdsm.common import libvirtconnection
from vdsm.common import supervdsm


DEFAULT_POOL = "ovirt"
LOCK_IMAGE_TEMPLATE = "ovirt-sd-%s-lock"
VG_TEMPLATE = "rbdlock-%s"
VOLUME_IMAGE_TEMPLATE = "volume-%s"
KEYRING_DIR = "/var/lib/vdsm/rbd"
RBD_SECRET_NAMESPACE = uuid.UUID("7902ebec-819d-4485-b680-91d0f53c36bf")

_runtime_connections = {}
_domain_connections = {}
_default_pool_connections = {}


class RBDCommandError(RuntimeError):
    pass


def configured_pools():
    pools = set(_default_pool_connections)
    pools.update(conn["pool"] for conn in _runtime_connections.values())
    env_pools = os.environ.get("VDSM_RBD_POOLS", DEFAULT_POOL)
    pools.update(pool.strip() for pool in env_pools.split(",")
                 if pool.strip())
    return sorted(pools)


def normalize_connection(params):
    if not params:
        params = {}
    elif isinstance(params, str):
        params = json.loads(params)
    params = dict(params)
    pool = (params.get("pool") or params.get("rbdPool") or
            params.get("poolName") or params.get("remotePath") or
            params.get("connection") or params.get("id") or DEFAULT_POOL)
    monitors = monitors_to_string(params.get("monitors") or
                                  params.get("rbdMonitors") or
                                  params.get("hosts") or
                                  params.get("portal") or
                                  params.get("iqn") or
                                  params.get("address") or "")
    ceph_user = normalize_ceph_user(params.get("cephUser") or
                                    params.get("ceph_user") or
                                    params.get("username") or
                                    params.get("userName") or
                                    params.get("user") or "")
    sd_uuid = (params.get("sdUUID") or params.get("storagedomainID") or
               params.get("storageDomainId") or params.get("domainID") or "")
    ceph_key = (params.get("cephKey") or params.get("ceph_key") or
                params.get("key") or params.get("password") or "")
    secret_uuid = (params.get("secretUUID") or params.get("secretUuid") or
                   params.get("libvirtSecretUUID") or "")
    if not secret_uuid:
        secret_uuid = default_secret_uuid(sd_uuid, pool, ceph_user)
    keyring = params.get("keyring") or params.get("keyringPath") or ""
    return {
        "id": params.get("id") or sd_uuid or pool,
        "pool": pool,
        "monitors": monitors,
        "cephUser": ceph_user,
        "cephKey": ceph_key,
        "sdUUID": sd_uuid,
        "secretUUID": str(secret_uuid),
        "keyring": keyring,
    }


def register_pool(pool, monitors=None, ceph_user=None, ceph_key=None):
    return register_connection({
        "id": pool,
        "pool": pool,
        "monitors": monitors,
        "cephUser": ceph_user,
        "cephKey": ceph_key,
    })


def register_connection(params):
    conn = normalize_connection(params)
    if conn.get("cephKey"):
        conn = ensure_connection_credentials(conn)
    store = _without_secret(conn)
    key = _connection_key(store["pool"], store.get("cephUser"))
    _runtime_connections[key] = store
    _default_pool_connections[store["pool"]] = store
    if store.get("sdUUID"):
        _domain_connections[store["sdUUID"]] = store
    return dict(store)


def unregister_connection(params):
    conn = normalize_connection(params)
    key = _connection_key(conn["pool"], conn.get("cephUser"))
    _runtime_connections.pop(key, None)
    if conn.get("sdUUID"):
        _domain_connections.pop(conn["sdUUID"], None)
    return _without_secret(conn)


def registered_config(pool):
    conn = connection_for_pool(pool)
    return {
        "pool": conn["pool"],
        "monitors": conn.get("monitors", ""),
        "ceph_user": conn.get("cephUser", ""),
        "secret_uuid": conn.get("secretUUID", ""),
        "keyring": conn.get("keyring", ""),
    }


def connection_for_pool(pool, ceph_user=None):
    user = normalize_ceph_user(ceph_user)
    if user:
        conn = _runtime_connections.get(_connection_key(pool, user))
        if conn:
            return conn
    conn = _default_pool_connections.get(pool)
    if conn:
        return conn
    return {
        "id": pool,
        "pool": pool,
        "monitors": monitors_to_string(os.environ.get(
            "VDSM_RBD_MONITORS", "")),
        "cephUser": normalize_ceph_user(os.environ.get(
            "VDSM_RBD_CEPH_USER", "")),
        "secretUUID": os.environ.get("VDSM_RBD_SECRET_UUID", ""),
        "keyring": os.environ.get("VDSM_RBD_KEYRING", ""),
        "sdUUID": "",
    }


def connection_for_domain(sd_uuid, pool=None, ceph_user=None):
    conn = _domain_connections.get(sd_uuid)
    if conn:
        return conn
    if pool is not None:
        return connection_for_pool(pool, ceph_user)
    return connection_for_pool(DEFAULT_POOL, ceph_user)


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


def monitors(pool=None, ceph_user=None):
    if pool is None:
        raw = os.environ.get("VDSM_RBD_MONITORS", "")
    else:
        raw = connection_for_pool(pool, ceph_user).get("monitors", "")
    return parse_monitors(raw)


def normalize_ceph_user(user):
    user = str(user or "").strip()
    if user.startswith("client."):
        user = user[len("client."):]
    return user


def ceph_user_for_libvirt(pool=None, ceph_user=None):
    user = normalize_ceph_user(ceph_user)
    if user:
        return user
    if pool is None:
        return normalize_ceph_user(os.environ.get(
            "VDSM_RBD_CEPH_USER", "ovirt"))
    return connection_for_pool(pool).get("cephUser") or "ovirt"


def libvirt_secret_uuid(pool=None, ceph_user=None, sd_uuid=None):
    if sd_uuid:
        conn = connection_for_domain(sd_uuid, pool, ceph_user)
        return conn.get("secretUUID") or domain_secret_uuid(sd_uuid)
    if pool is None:
        return os.environ.get("VDSM_RBD_SECRET_UUID", "")
    conn = connection_for_pool(pool, ceph_user)
    return conn.get("secretUUID", "")


def libvirt_auth(pool=None, ceph_user=None, secret_uuid=None, sd_uuid=None):
    user = ceph_user_for_libvirt(pool, ceph_user)
    secret_uuid = (secret_uuid or
                   libvirt_secret_uuid(pool, user, sd_uuid=sd_uuid))
    if not secret_uuid:
        return None
    return {
        "username": user,
        "secretUUID": secret_uuid,
    }


def domain_secret_uuid(sd_uuid):
    return str(uuid.uuid5(RBD_SECRET_NAMESPACE, "domain:%s" % sd_uuid))


def default_secret_uuid(sd_uuid, pool, ceph_user):
    if sd_uuid:
        return domain_secret_uuid(sd_uuid)
    material = "pool:%s:%s" % (pool, normalize_ceph_user(ceph_user))
    return str(uuid.uuid5(RBD_SECRET_NAMESPACE, material))


def ensure_connection_credentials(conn):
    conn = dict(conn)
    secret_uuid = conn.get("secretUUID") or default_secret_uuid(
        conn.get("sdUUID"), conn["pool"], conn.get("cephUser"))
    conn["secretUUID"] = secret_uuid
    ensure_libvirt_secret(
        secret_uuid,
        conn.get("cephUser"),
        conn.get("cephKey"),
        conn.get("sdUUID") or conn["pool"],
    )
    conn["keyring"] = write_keyring(
        conn.get("sdUUID") or secret_uuid,
        conn.get("cephUser"),
        conn.get("cephKey"),
    )
    return conn


def ensure_libvirt_secret(secret_uuid, ceph_user, ceph_key, label):
    if not ceph_key:
        return secret_uuid
    user = normalize_ceph_user(ceph_user) or "ovirt"
    conn = libvirtconnection.get()
    xml = _secret_xml(secret_uuid, user, label)
    secret = conn.secretDefineXML(xml)
    secret.setValue(_decode_ceph_key(ceph_key), 0)
    return secret_uuid


def write_keyring(identity, ceph_user, ceph_key):
    return supervdsm.getProxy().rbd_write_ceph_keyring(
        str(identity), normalize_ceph_user(ceph_user), str(ceph_key))


def lock_image_name(sd_uuid):
    return LOCK_IMAGE_TEMPLATE % sd_uuid


def vg_name(sd_uuid):
    return VG_TEMPLATE % sd_uuid


def volume_image_name(vol_uuid):
    return VOLUME_IMAGE_TEMPLATE % vol_uuid


def rbd_spec(pool, image):
    return "%s/%s" % (pool, image)


def list_images(pool, ceph_user=None):
    out = _run(_rbd_cmd(pool, ceph_user) + ["ls", pool])
    return out.decode("utf-8").splitlines()


def image_exists(pool, image, ceph_user=None):
    try:
        _run(_rbd_cmd(pool, ceph_user) + [
            "info", rbd_spec(pool, image)])
        return True
    except RBDCommandError:
        return False


def image_info(pool, image, ceph_user=None):
    out = _run(_rbd_cmd(pool, ceph_user) + [
        "info", rbd_spec(pool, image), "--format", "json"])
    return json.loads(out.decode("utf-8"))


def image_size(pool, image, ceph_user=None):
    return int(image_info(pool, image, ceph_user=ceph_user)["size"])


def list_volume_images(pool, ceph_user=None):
    return [name for name in list_images(pool, ceph_user=ceph_user)
            if name.startswith("volume-")]


def create_image(pool, image, size_bytes, ceph_user=None):
    size_mib = int(size_bytes // 1024 // 1024)
    if size_mib <= 0:
        raise ValueError("RBD image size must be positive")
    _run(_rbd_cmd(pool, ceph_user) + [
        "create", rbd_spec(pool, image),
        "--size", str(size_mib),
        "--image-format", "2",
    ])


def remove_image(pool, image, ceph_user=None):
    _run(_rbd_cmd(pool, ceph_user) + [
        "rm", rbd_spec(pool, image), "--no-progress"])


def resize_image(pool, image, size_bytes, ceph_user=None):
    size_mib = int(size_bytes // 1024 // 1024)
    if size_mib <= 0:
        raise ValueError("RBD image size must be positive")
    _run(_rbd_cmd(pool, ceph_user) + [
        "resize", rbd_spec(pool, image), "--size", str(size_mib)])


def set_image_meta(pool, image, values, ceph_user=None):
    for key, value in values.items():
        if value is None:
            value = ""
        _run(_rbd_cmd(pool, ceph_user) + [
            "image-meta", "set", rbd_spec(pool, image),
            str(key), str(value),
        ])


def get_image_meta(pool, image, key, default=None, ceph_user=None):
    try:
        out = _run(_rbd_cmd(pool, ceph_user) + [
            "image-meta", "get", rbd_spec(pool, image), key])
    except RBDCommandError:
        return default
    return out.decode("utf-8").strip()


def get_image_metadata(pool, image, ceph_user=None):
    try:
        out = _run(_rbd_cmd(pool, ceph_user) + [
            "image-meta", "list", rbd_spec(pool, image),
            "--format", "json",
        ])
    except RBDCommandError:
        return {}
    return json.loads(out.decode("utf-8"))


def map_lock_image(pool, image, ceph_user=None):
    conn = connection_for_pool(pool, ceph_user)
    user = ceph_user or conn.get("cephUser")
    return supervdsm.getProxy().rbd_map_lock_image(
        pool, image, user, conn.get("keyring", ""))


def unmap_lock_image(path):
    return supervdsm.getProxy().rbd_unmap_lock_image(path)


def create_domain_layout(pool, sd_uuid, lock_size_mib, lv_sizes,
                         ceph_user=None):
    conn = connection_for_domain(sd_uuid, pool, ceph_user)
    user = ceph_user or conn.get("cephUser")
    return supervdsm.getProxy().rbd_create_domain_layout(
        pool,
        sd_uuid,
        int(lock_size_mib),
        list(lv_sizes),
        user,
        conn.get("keyring", ""),
    )


def remove_domain_layout(pool, sd_uuid, ceph_user=None):
    conn = connection_for_domain(sd_uuid, pool, ceph_user)
    user = ceph_user or conn.get("cephUser")
    return supervdsm.getProxy().rbd_remove_domain_layout(
        pool, sd_uuid, user, conn.get("keyring", ""))


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


def pool_stats(pool, ceph_user=None):
    try:
        out = _run(_ceph_cmd(pool, ceph_user) + ["df", "--format", "json"])
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


def _rbd_cmd(pool=None, ceph_user=None):
    cmd = ["rbd"]
    conn = connection_for_pool(pool, ceph_user) if pool else {}
    user = ceph_user or conn.get("cephUser")
    user = normalize_ceph_user(user)
    if user:
        cmd.extend(["--id", user])
    keyring = conn.get("keyring")
    if keyring:
        cmd.extend(["--keyring", keyring])
    return cmd


def _ceph_cmd(pool=None, ceph_user=None):
    cmd = ["ceph"]
    conn = connection_for_pool(pool, ceph_user) if pool else {}
    user = ceph_user or conn.get("cephUser")
    user = normalize_ceph_user(user)
    if user:
        cmd.extend(["--id", user])
    keyring = conn.get("keyring")
    if keyring:
        cmd.extend(["--keyring", keyring])
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


def _connection_key(pool, ceph_user=None):
    return pool, normalize_ceph_user(ceph_user)


def _without_secret(conn):
    store = dict(conn)
    store.pop("cephKey", None)
    return store


def _secret_xml(secret_uuid, ceph_user, label):
    name = "client.%s rbd-%s" % (ceph_user, label)
    return (
        "<secret ephemeral='no' private='yes'>"
        "<uuid>%s</uuid>"
        "<usage type='ceph'><name>%s</name></usage>"
        "</secret>"
    ) % (escape(str(secret_uuid)), escape(name))


def _decode_ceph_key(ceph_key):
    key = str(ceph_key or "").strip()
    try:
        return base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        return key.encode("utf-8")


def _run(cmd):
    try:
        return commands.run(cmd)
    except cmdutils.Error as e:
        raise RBDCommandError("RBD command failed: %r: %s" % (cmd, e))

