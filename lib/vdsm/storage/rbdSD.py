# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Experimental native Ceph RBD storage domain.

Layout used by this backend:

    pool/ovirt-sd-<sdUUID>-lock
        mapped by krbd on every host
        contains VG rbdlock-<sdUUID>
            ids, leases, xleases, metadata, inbox, outbox, master

    pool/volume-<volUUID>
        VM data disks, passed to libvirt as network protocol='rbd'

The important design choice is that VM/data volume leases are allocated through
sd.StorageDomain.create_lease() and xlease.LeasesVolume, not by a separate RBD
allocator.  This keeps the persistent lease_id -> offset mapping in xleases.
"""

import logging
import os

from vdsm import constants
from vdsm.storage import blockSD
from vdsm.storage import clusterlock
from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import fileUtils
from vdsm.storage import lvm
from vdsm.storage import misc
from vdsm.storage import mount
from vdsm.storage import rbd_utils
from vdsm.storage import rbdVolume
from vdsm.storage import sd


log = logging.getLogger("storage.rbdsd")

RBD_SD_DIR = "rbdSD"
LOCK_IMAGE_PREFIX = "ovirt-sd-"
LOCK_IMAGE_SUFFIX = "-lock"
DEFAULT_LOCK_SIZE_MIB = 8192

RBD_META_MONITORS = "rbd.monitors"
RBD_META_CEPH_USER = "rbd.cephUser"
RBD_META_SECRET_UUID = "rbd.secretUUID"
RBD_META_KEYRING_PATH = "rbd.keyringPath"
RBD_META_SD_UUID = "rbd.sdUUID"


def createDomain(sdUUID, domainName, domClass=sd.DATA_DOMAIN, pool=None,
                 version=5, block_size=sc.BLOCK_SIZE_512,
                 max_hosts=sc.HOSTS_4K_1M,
                 lock_size_mib=DEFAULT_LOCK_SIZE_MIB, monitors=None,
                 ceph_user=None, ceph_key=None, secret_uuid=None):
    """Create a native RBD storage domain."""
    return RBDStorageDomain.create(
        sdUUID,
        domainName,
        domClass,
        pool=pool,
        version=version,
        block_size=block_size,
        max_hosts=max_hosts,
        lock_size_mib=lock_size_mib,
        monitors=monitors,
        ceph_user=ceph_user,
        ceph_key=ceph_key,
        secret_uuid=secret_uuid,
    )


def create_rbd_domain(*args, **kwargs):
    return createDomain(*args, **kwargs)


def connectStorageServer(conList):
    """Register RBD connection parameters passed by Engine.

    RBD has no mount/login step equivalent to NFS/iSCSI here.  If Engine
    provides cephKey and sdUUID, VDSM also installs the per-domain libvirt
    secret and root-only Ceph keyring on this host.
    """
    results = []
    for params in conList:
        raw = rbd_utils.normalize_connection(params)
        if raw.get("cephKey") and raw.get("sdUUID"):
            conn = rbd_utils.prepare_domain_credentials(
                raw["sdUUID"],
                raw["pool"],
                monitors=raw.get("monitors"),
                ceph_user=raw.get("cephUser"),
                ceph_key=raw.get("cephKey"),
                secret_uuid=raw.get("secretUUID"),
            )
        else:
            conn = rbd_utils.register_connection(raw)
        try:
            rbd_utils.list_images(
                conn["pool"],
                ceph_user=conn.get("cephUser"),
                keyring_path=conn.get("keyringPath"),
                monitors=conn.get("monitors"),
                sd_uuid=conn.get("sdUUID"),
            )
        except Exception:
            log.warning("Cannot connect RBD pool %s", conn["pool"],
                        exc_info=True)
            results.append({"id": conn["id"], "status": 1})
        else:
            results.append({"id": conn["id"], "status": 0})
    return {"statuslist": results}


def disconnectStorageServer(conList):
    results = []
    for params in conList:
        conn = rbd_utils.unregister_connection(params)
        results.append({"id": conn["id"], "status": 0})
    return {"statuslist": results}


class RBDStorageDomainManifest(sd.StorageDomainManifest):
    """Manifest backed by metadata tags on the lock-image VG."""

    mountpoint = os.path.join(sc.REPO_MOUNT_DIR, RBD_SD_DIR)

    def __init__(self, sdUUID, pool, lock_image, lock_device=None,
                 monitors=None, ceph_user=None, secret_uuid=None,
                 keyring_path=None):
        self.pool = pool
        self.lock_image = lock_image
        self.lock_device = lock_device
        self.vg_name = rbd_utils.vg_name(sdUUID)
        conn = rbd_utils.connection_for_pool(pool, sdUUID)
        self._monitors = monitors or conn.get("monitors", "")
        self._ceph_user = ceph_user or conn.get("cephUser", "")
        self._secret_uuid = secret_uuid or conn.get("secretUUID", "")
        if not self._secret_uuid and self._ceph_user:
            self._secret_uuid = rbd_utils.domain_secret_uuid(
                sdUUID, self._ceph_user)
        self._keyring_path = keyring_path or conn.get("keyringPath", "")
        if not self._keyring_path and self._ceph_user:
            self._keyring_path = rbd_utils.domain_keyring_path(
                sdUUID, self._ceph_user)
        domaindir = os.path.join(self.mountpoint, sdUUID)
        metadata = blockSD.TagBasedSDMetadata(self.vg_name)
        super(RBDStorageDomainManifest, self).__init__(
            sdUUID, domaindir, metadata)
        self._load_rbd_metadata()

    @classmethod
    def supports_external_leases(cls, version):
        return version >= 4

    @classmethod
    def special_volumes(cls, version):
        if version >= 4:
            return sd.SPECIAL_VOLUMES_V4 + (blockSD.MASTERLV,)
        return sd.SPECIAL_VOLUMES_V0 + (blockSD.MASTERLV,)

    def getStorageType(self):
        return sd.RBD_DOMAIN

    def setup(self):
        self.lock_device = rbd_utils.map_lock_image(
            self.pool, self.lock_image, self._ceph_user,
            keyring_path=self._keyring_path, monitors=self._monitors,
            sd_uuid=self.sdUUID)
        rbd_utils.activate_vg(self.vg_name)

    def teardown(self):
        rbd_utils.deactivate_vg(self.vg_name)
        if self.lock_device:
            rbd_utils.unmap_lock_image(self.lock_device)
            self.lock_device = None

    def rbd_monitors(self):
        return rbd_utils.monitors_from_string(self._monitors)

    def rbd_auth(self):
        return self.libvirt_auth()

    def libvirt_auth(self):
        return rbd_utils.libvirt_auth(
            self.pool, self._ceph_user, self._secret_uuid,
            sd_uuid=self.sdUUID)

    def ceph_user(self):
        return self._ceph_user

    def keyring_path(self):
        return self._keyring_path

    def monitors(self):
        return rbd_utils.monitors_from_string(self._monitors)

    def monitors_string(self):
        return self._monitors

    def secret_uuid(self):
        return self._secret_uuid

    def _rbd_kwargs(self):
        return {
            "ceph_user": self._ceph_user,
            "keyring_path": self._keyring_path,
            "monitors": self._monitors,
            "sd_uuid": self.sdUUID,
        }

    def _load_rbd_metadata(self):
        self._monitors = rbd_utils.get_image_meta(
            self.pool, self.lock_image, RBD_META_MONITORS, self._monitors,
            **self._rbd_kwargs())
        self._ceph_user = rbd_utils.get_image_meta(
            self.pool, self.lock_image, RBD_META_CEPH_USER, self._ceph_user,
            **self._rbd_kwargs())
        self._secret_uuid = rbd_utils.get_image_meta(
            self.pool, self.lock_image, RBD_META_SECRET_UUID,
            self._secret_uuid, **self._rbd_kwargs())
        self._keyring_path = rbd_utils.get_image_meta(
            self.pool, self.lock_image, RBD_META_KEYRING_PATH,
            self._keyring_path, **self._rbd_kwargs())
        if not self._secret_uuid and self._ceph_user:
            self._secret_uuid = rbd_utils.domain_secret_uuid(
                self.sdUUID, self._ceph_user)
        if not self._keyring_path and self._ceph_user:
            self._keyring_path = rbd_utils.domain_keyring_path(
                self.sdUUID, self._ceph_user)

    def _lv_path(self, name):
        try:
            lvm.activateLVs(self.vg_name, [name])
        except Exception:
            log.debug(
                "Could not explicitly activate %s/%s",
                self.vg_name, name, exc_info=True)
        return rbd_utils.lv_path(self.vg_name, name)

    def getIdsFilePath(self):
        return self._lv_path(sd.IDS)

    def getLeasesFilePath(self):
        return self._lv_path(sd.LEASES)

    def external_leases_path(self):
        return self._lv_path(sd.XLEASES)

    def getMonitoringPath(self):
        return self._lv_path(sd.METADATA)

    def getVolumeClass(self):
        return rbdVolume.RBDVolumeManifest

    def getVolumeLease(self, imgUUID, volUUID):
        try:
            lease_info = self.lease_info(volUUID)
        except se.NoSuchLease:
            return clusterlock.Lease(None, None, None)
        return clusterlock.Lease(
            lease_info.resource,
            lease_info.path,
            lease_info.offset,
        )

    def getVolumeSize(self, imgUUID, volUUID):
        size = rbd_utils.image_size(
            self.pool, rbd_utils.volume_image_name(volUUID),
            **self._rbd_kwargs())
        return sd.VolumeSize(apparentsize=size, truesize=size)

    def getVSize(self, imgUUID, volUUID):
        return self.getVolumeSize(imgUUID, volUUID).apparentsize

    def getVAllocSize(self, imgUUID, volUUID):
        return self.getVolumeSize(imgUUID, volUUID).truesize

    def getAllImages(self):
        images = set()
        for image in rbd_utils.list_volume_images(
                self.pool, **self._rbd_kwargs()):
            img_uuid = rbd_utils.get_image_meta(
                self.pool, image, "imgUUID", **self._rbd_kwargs())
            if img_uuid:
                images.add(img_uuid)
        return list(images)

    def getAllVolumes(self):
        volumes = {}
        for image in rbd_utils.list_volume_images(
                self.pool, **self._rbd_kwargs()):
            vol_uuid = image[len("volume-"):]
            img_uuid = rbd_utils.get_image_meta(
                self.pool, image, "imgUUID", "", **self._rbd_kwargs())
            parent = rbd_utils.get_image_meta(
                self.pool, image, "parent", sd.BLANK_UUID,
                **self._rbd_kwargs())
            volumes[vol_uuid] = sd.ImgsPar([img_uuid], parent)
        return volumes

    def refreshDirTree(self):
        if not os.path.isdir(self.domaindir):
            os.makedirs(self.domaindir)

    def refresh(self):
        pass


class RBDStorageDomain(sd.StorageDomain):
    """StorageDomain implementation for native RBD images."""

    manifestClass = RBDStorageDomainManifest
    supported_block_size = (sc.BLOCK_SIZE_512,)
    supported_versions = tuple(
        v for v in sc.SUPPORTED_DOMAIN_VERSIONS if v >= 4)

    @classmethod
    def create_from_api(cls, sdUUID, domainName, domClass, typeSpecificArg,
                        storageType, domVersion,
                        block_size=sc.BLOCK_SIZE_512,
                        max_hosts=sc.HOSTS_4K_1M):
        if storageType != sd.RBD_DOMAIN:
            raise se.StorageDomainTypeError(storageType)
        args = _parse_type_args(typeSpecificArg)
        return cls.create(
            sdUUID,
            domainName,
            domClass,
            pool=args["pool"],
            version=domVersion,
            block_size=block_size,
            max_hosts=max_hosts,
            lock_size_mib=args["lock_size_mib"],
            monitors=args["monitors"],
            ceph_user=args["ceph_user"],
            ceph_key=args["ceph_key"],
            secret_uuid=args["secret_uuid"],
        )

    @classmethod
    def create(cls, sdUUID, domainName, domClass=sd.DATA_DOMAIN, pool=None,
               version=5, block_size=sc.BLOCK_SIZE_512,
               max_hosts=sc.HOSTS_4K_1M,
               lock_size_mib=DEFAULT_LOCK_SIZE_MIB, monitors=None,
               ceph_user=None, ceph_key=None, secret_uuid=None):
        cls._validate_create_params(domainName, domClass, version, block_size)

        if pool is None:
            pool = rbd_utils.configured_pools()[0]

        raw_conn = {
            "pool": pool,
            "sdUUID": sdUUID,
            "monitors": monitors,
            "cephUser": ceph_user,
            "cephKey": ceph_key,
            "secretUUID": secret_uuid,
        }
        if ceph_key:
            conn = rbd_utils.prepare_domain_credentials(
                sdUUID,
                pool,
                monitors=monitors,
                ceph_user=ceph_user,
                ceph_key=ceph_key,
                secret_uuid=secret_uuid,
            )
        else:
            conn = rbd_utils.register_connection(raw_conn)
        pool = conn["pool"]
        monitors = conn["monitors"]
        ceph_user = conn["cephUser"]
        secret_uuid = conn["secretUUID"]
        keyring_path = conn.get("keyringPath", "")

        alignment = clusterlock.alignment(block_size, max_hosts)
        vg_name = rbd_utils.vg_name(sdUUID)
        lock_image = rbd_utils.lock_image_name(sdUUID)
        lv_sizes = cls._special_volume_sizes_mib(alignment)

        rbd_utils.create_domain_layout(
            pool, sdUUID, lock_size_mib, sorted(lv_sizes.items()),
            ceph_user=ceph_user, keyring_path=keyring_path,
            monitors=monitors)

        try:
            rbd_utils.set_image_meta(pool, lock_image, {
                RBD_META_SD_UUID: sdUUID,
                RBD_META_MONITORS: monitors,
                RBD_META_CEPH_USER: ceph_user,
                RBD_META_SECRET_UUID: secret_uuid,
                RBD_META_KEYRING_PATH: keyring_path,
            }, ceph_user=ceph_user, keyring_path=keyring_path,
                monitors=monitors, sd_uuid=sdUUID)
            xleases_path = rbd_utils.lv_path(vg_name, sd.XLEASES)
            cls.format_external_leases(
                sdUUID,
                xleases_path,
                alignment=alignment,
                block_size=block_size,
            )
            cls._write_initial_metadata(
                sdUUID,
                domainName,
                domClass,
                version,
                block_size,
                alignment,
            )
            manifest = RBDStorageDomainManifest(
                sdUUID, pool, lock_image, monitors=monitors,
                ceph_user=ceph_user, secret_uuid=secret_uuid,
                keyring_path=keyring_path)
            domain = cls(manifest)
            domain.refreshDirTree()
            domain.initSPMlease()
            return domain
        except Exception:
            log.error("Rolling back failed RBD domain %s", sdUUID,
                      exc_info=True)
            rbd_utils.remove_domain_layout(
                pool, sdUUID, ceph_user=ceph_user,
                keyring_path=keyring_path, monitors=monitors)
            raise

    @classmethod
    def _validate_create_params(cls, domainName, domClass, version,
                                block_size):
        if block_size == sc.BLOCK_SIZE_AUTO:
            block_size = sc.BLOCK_SIZE_512
        cls.validate_version(version)
        if version < 4:
            raise se.UnsupportedDomainVersion(version)
        if block_size not in cls.supported_block_size:
            raise se.UnsupportedOperation(
                "Unsupported RBD storage domain block size",
                block_size=block_size)
        if len(domainName) > sd.MAX_DOMAIN_DESCRIPTION_SIZE:
            raise se.StorageDomainDescriptionTooLongError()
        if domClass not in (sd.DATA_DOMAIN, sd.ISO_DOMAIN, sd.BACKUP_DOMAIN):
            raise se.InvalidParameterException("domClass", domClass)

    @classmethod
    def _special_volume_sizes_mib(cls, alignment):
        alignment_mib = alignment // (1024 * 1024)
        sizes = dict(sd.SPECIAL_VOLUME_SIZES_MIB)
        sizes[sd.METADATA] = blockSD.METADATA_LV_SIZE_MB
        sizes[sd.LEASES] = sd.LEASES_SLOTS * alignment_mib
        sizes[sd.XLEASES] = sd.XLEASES_SLOTS * alignment_mib
        sizes[blockSD.MASTERLV] = blockSD.MASTER_LV_SIZE_MB
        return sizes

    @classmethod
    def _write_initial_metadata(cls, sdUUID, domainName, domClass, version,
                                block_size, alignment):
        vg_name = rbd_utils.vg_name(sdUUID)
        metadata = blockSD.TagBasedSDMetadata(vg_name)
        initial = {
            sd.DMDK_VERSION: version,
            sd.DMDK_SDUUID: sdUUID,
            sd.DMDK_TYPE: sd.RBD_DOMAIN,
            sd.DMDK_CLASS: domClass,
            sd.DMDK_DESCRIPTION: domainName,
            sd.DMDK_ROLE: sd.REGULAR_DOMAIN,
            sd.DMDK_POOLS: [],
            sd.DMDK_LOCK_POLICY: "",
            sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC:
                sd.DEFAULT_LEASE_PARAMS[
                    sd.DMDK_LOCK_RENEWAL_INTERVAL_SEC],
            sd.DMDK_LEASE_TIME_SEC:
                sd.DEFAULT_LEASE_PARAMS[sd.DMDK_LEASE_TIME_SEC],
            sd.DMDK_IO_OP_TIMEOUT_SEC:
                sd.DEFAULT_LEASE_PARAMS[sd.DMDK_IO_OP_TIMEOUT_SEC],
            sd.DMDK_LEASE_RETRIES:
                sd.DEFAULT_LEASE_PARAMS[sd.DMDK_LEASE_RETRIES],
            blockSD.DMDK_VGUUID: rbd_utils.vg_uuid(vg_name),
        }
        if version < 5:
            initial[sd.DMDK_LOGBLKSIZE] = block_size
            initial[sd.DMDK_PHYBLKSIZE] = block_size
        else:
            initial[sd.DMDK_ALIGNMENT] = alignment
            initial[sd.DMDK_BLOCK_SIZE] = block_size
        metadata.update(initial)

    def setup(self):
        self._manifest.setup()

    def teardown(self):
        self._manifest.teardown()

    def getVolumeClass(self):
        return rbdVolume.RBDVolume

    def createVolume(self, imgUUID, capacity, volFormat, preallocate,
                     diskType, volUUID, desc, srcImgUUID, srcVolUUID,
                     initial_size=None, add_bitmaps=False, legal=True,
                     sequence=0, bitmap=None):
        self.validateCreateVolumeParams(
            volFormat,
            srcVolUUID,
            diskType=diskType,
            preallocate=preallocate,
            add_bitmaps=add_bitmaps,
            bitmap=bitmap,
        )
        return rbdVolume.RBDVolume.create(
            self,
            imgUUID,
            capacity,
            volFormat,
            preallocate,
            diskType,
            volUUID,
            desc,
            srcImgUUID,
            srcVolUUID,
            initial_size=initial_size,
            legal=legal,
            sequence=sequence,
        )

    def create_volume_lease(self, volUUID):
        self.create_lease(volUUID)

    def delete_volume_lease(self, volUUID):
        self.delete_lease(volUUID)

    def validate(self):
        try:
            lvm.chkVG(self._manifest.vg_name)
        except se.LVMCommandError as e:
            raise se.StorageDomainAccessError(self.sdUUID, reason=e)
        self.invalidateMetadata()
        if not len(self.getMetadata()):
            raise se.StorageDomainAccessError(self.sdUUID)

    def getInfo(self):
        info = sd.StorageDomain.getInfo(self)
        vg = lvm.getVG(self._manifest.vg_name)
        info["vguuid"] = vg.uuid
        info["state"] = vg.partial
        info["remotePath"] = self.getRemotePath()
        info["rbdPool"] = self._manifest.pool
        info["rbdLockImage"] = self._manifest.lock_image
        return info

    def getStats(self):
        free, total = rbd_utils.pool_capacity(
            self._manifest.pool, **self._manifest._rbd_kwargs())
        mdasize = blockSD.METADATA_LV_SIZE_MB * 1024 * 1024
        return {
            "disktotal": total,
            "diskfree": free,
            "mdasize": mdasize,
            "mdafree": mdasize,
            "mdavalid": True,
            "mdathreshold": False,
        }

    def validateMasterMount(self):
        return mount.isMounted(self.getMasterDir())

    def mountMaster(self):
        lvm.activateLVs(self._manifest.vg_name, [blockSD.MASTERLV],
                        refresh=False)
        master_dir = self.getMasterDir()
        self.log.info("Creating domain master directory %r", master_dir)
        fileUtils.createdir(master_dir)
        master_dev = rbd_utils.lv_path(self._manifest.vg_name,
                                       blockSD.MASTERLV)
        rc, _out, _err = misc.execCmd([constants.EXT_FSCK, "-p", master_dev],
                                      sudo=True)
        if rc == 1 or rc == 2:
            self.log.info("fsck corrected fs errors (%s)", rc)
        if rc >= 4:
            raise se.BlockStorageDomainMasterFSCKError(master_dev, rc)
        misc.execCmd([constants.EXT_TUNE2FS, "-j", master_dev], sudo=True)
        master_mount = mount.Mount(master_dev, master_dir)
        try:
            master_mount.mount(vfstype=mount.VFS_EXT3)
        except mount.MountError as e:
            raise se.BlockStorageDomainMasterMountError(
                master_dev, e.rc, e.out, e.err)
        cmd = [
            constants.EXT_CHOWN,
            "%s:%s" % (constants.METADATA_USER,
                        constants.METADATA_GROUP),
            master_dir,
        ]
        rc, _out, _err = misc.execCmd(cmd, sudo=True)
        if rc != 0:
            self.log.error("failed to chown %s", master_dir)

    def unmountMaster(self):
        master_dir = self.getMasterDir()
        blockSD.BlockStorageDomain.doUnmountMaster(master_dir)
        lvm.deactivateLVs(self._manifest.vg_name, [blockSD.MASTERLV])

    @classmethod
    def format(cls, sdUUID):
        for pool in rbd_utils.configured_pools():
            lock_image = rbd_utils.lock_image_name(sdUUID)
            conn = rbd_utils.connection_for_pool(pool, sdUUID)
            if not rbd_utils.image_exists(
                    pool, lock_image, ceph_user=conn.get("cephUser"),
                    keyring_path=conn.get("keyringPath"),
                    monitors=conn.get("monitors"), sd_uuid=sdUUID):
                continue
            rbd_utils.remove_domain_layout(
                pool, sdUUID, ceph_user=conn.get("cephUser"),
                keyring_path=conn.get("keyringPath"),
                monitors=conn.get("monitors"))
            try:
                rbd_utils.remove_domain_credentials(
                    sdUUID, ceph_user=conn.get("cephUser"),
                    secret_uuid=conn.get("secretUUID"))
            except Exception:
                log.warning("Cannot remove RBD credentials for %s", sdUUID,
                            exc_info=True)
            try:
                fileUtils.cleanupdir(cls.findDomainPath(sdUUID),
                                     ignoreErrors=True)
            except Exception:
                log.warning("Cannot remove RBD domain dir %s", sdUUID,
                            exc_info=True)
            return True
        raise se.StorageDomainDoesNotExist(sdUUID)

    @staticmethod
    def findDomainPath(sdUUID):
        return os.path.join(RBDStorageDomainManifest.mountpoint, sdUUID)

    def getRemotePath(self):
        return "%s/%s" % (self._manifest.pool, self._manifest.lock_image)


def getStorageDomainsList():
    uuids = []
    for pool in rbd_utils.configured_pools():
        try:
            images = rbd_utils.list_images(pool)
        except Exception:
            log.warning("Cannot list RBD pool %s", pool, exc_info=True)
            continue
        for image in images:
            sd_uuid = _parse_lock_image(image)
            if sd_uuid:
                uuids.append(sd_uuid)
    return uuids


def findDomain(sdUUID):
    for pool in rbd_utils.configured_pools():
        lock_image = rbd_utils.lock_image_name(sdUUID)
        conn = rbd_utils.connection_for_pool(pool, sdUUID)
        if not rbd_utils.image_exists(
                pool, lock_image, ceph_user=conn.get("cephUser"),
                keyring_path=conn.get("keyringPath"),
                monitors=conn.get("monitors"), sd_uuid=sdUUID):
            continue
        lock_device = rbd_utils.map_lock_image(
            pool, lock_image, conn.get("cephUser"),
            keyring_path=conn.get("keyringPath"),
            monitors=conn.get("monitors"), sd_uuid=sdUUID)
        rbd_utils.activate_vg(rbd_utils.vg_name(sdUUID))
        manifest = RBDStorageDomainManifest(
            sdUUID,
            pool,
            lock_image,
            lock_device=lock_device,
            monitors=conn.get("monitors"),
            ceph_user=conn.get("cephUser"),
            secret_uuid=conn.get("secretUUID"),
            keyring_path=conn.get("keyringPath"),
        )
        return RBDStorageDomain(manifest)
    raise se.StorageDomainDoesNotExist(sdUUID)


def _parse_type_args(type_args):
    conn = rbd_utils.normalize_connection(type_args or {})
    lock_size = (type_args or {}).get("lockSizeMiB")
    if lock_size is None:
        lock_size = (type_args or {}).get("lockSizeMB")
    if lock_size is None:
        lock_size = DEFAULT_LOCK_SIZE_MIB
    conn["lock_size_mib"] = int(lock_size)
    conn["monitors"] = rbd_utils.normalize_monitors(conn["monitors"])
    conn["ceph_user"] = conn.pop("cephUser")
    conn["ceph_key"] = conn.pop("cephKey")
    conn["secret_uuid"] = conn.pop("secretUUID")
    return conn


def _parse_lock_image(image):
    if not image.startswith(LOCK_IMAGE_PREFIX):
        return None
    if not image.endswith(LOCK_IMAGE_SUFFIX):
        return None
    return image[len(LOCK_IMAGE_PREFIX):-len(LOCK_IMAGE_SUFFIX)]
