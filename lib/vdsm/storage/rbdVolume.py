# SPDX-FileCopyrightText: Red Hat, Inc.
# SPDX-License-Identifier: GPL-2.0-or-later

"""Experimental RBD volume implementation.

This MVP stores volume metadata in RBD image metadata and exposes the disk to
VMs as a libvirt network disk using protocol='rbd'.  It supports only raw leaf
volumes in this draft; snapshots, live merge and template clone flows are left
for follow-up patches.
"""

import logging
import time

from vdsm.storage import constants as sc
from vdsm.storage import exception as se
from vdsm.storage import rbd_utils
from vdsm.storage import sd
from vdsm.storage import volume
from vdsm.storage.sdc import sdCache


log = logging.getLogger("storage.rbdvolume")


class RBDVolumeManifest(volume.VolumeManifest):
    DISK_TYPE = "network"
    align_size = 1024 * 1024

    def __init__(self, repoPath, sdUUID, imgUUID, volUUID):
        super(RBDVolumeManifest, self).__init__(
            repoPath, sdUUID, imgUUID, volUUID)
        self.pool = _pool_for_domain(sdUUID)
        self.rbd_image = rbd_utils.volume_image_name(volUUID)
        self._volumePath = "%s/%s" % (self.pool, self.rbd_image)

    @classmethod
    def is_block(cls):
        return False

    @classmethod
    def zero_initialized(cls):
        # RBD newly-created extents read as zeroes.
        return True

    @property
    def _domain_manifest(self):
        return sdCache.produce_manifest(self.sdUUID)

    def _ceph_user(self):
        return self._domain_manifest.ceph_user()

    def validateImagePath(self):
        # No file-system image directory exists for native RBD data images.
        return True

    def validateVolumePath(self):
        if not rbd_utils.image_exists(
                self.pool, self.rbd_image, ceph_user=self._ceph_user()):
            raise se.VolumeDoesNotExist(self.volUUID)

    def getVolumePath(self):
        # For libvirt network disks this is the source name, not a POSIX path.
        return self._volumePath

    def getMetadataId(self):
        return self.pool, self.rbd_image

    def getMetadata(self, metaId=None):
        meta = rbd_utils.get_image_metadata(
            self.pool, self.rbd_image, ceph_user=self._ceph_user())
        if not meta:
            raise se.VolumeMetadataReadError(self.volUUID)
        return meta

    def setMetadata(self, meta, metaId=None, **overrides):
        values = dict(meta)
        values.update(overrides)
        rbd_utils.set_image_meta(
            self.pool, self.rbd_image, values, ceph_user=self._ceph_user())

    def removeMetadata(self, metaId=None):
        # RBD image metadata is removed with the image.  Keeping this as a
        # no-op allows generic cleanup code to call it safely.
        pass

    @classmethod
    def _putMetadata(cls, metaId, meta, **overrides):
        pool, image = metaId
        values = dict(meta)
        values.update(overrides)
        rbd_utils.set_image_meta(pool, image, values)

    def getParent(self):
        return self.getMetadata().get(sc.PUUID, sd.BLANK_UUID)

    def getChildren(self):
        # Snapshot/clone support is intentionally not part of the first MVP.
        return []

    def getImage(self):
        return self.getMetadata().get(sc.IMAGE, self.imgUUID)

    def getVolumeSize(self):
        capacity = self.getMetadata().get(sc.CAPACITY)
        if capacity is not None:
            return int(capacity)
        return rbd_utils.image_size(
            self.pool, self.rbd_image, ceph_user=self._ceph_user())

    def getVolumeTrueSize(self):
        return rbd_utils.image_size(
            self.pool, self.rbd_image, ceph_user=self._ceph_user())

    def getFormat(self):
        return sc.name2type(self.getMetadata().get(sc.FORMAT, "RAW"))

    def getType(self):
        return sc.name2type(self.getMetadata().get(sc.TYPE, "SPARSE"))

    def getDiskType(self):
        return self.getMetadata().get(sc.DISKTYPE, sc.DATA_DISKTYPE)

    def getDescription(self):
        return self.getMetadata().get(sc.DESCRIPTION, "")

    def getLegality(self):
        return self.getMetadata().get(sc.LEGALITY, sc.LEGAL_VOL)

    def getVolType(self):
        return sc.name2type(self.getMetadata().get(sc.VOLTYPE, "LEAF"))

    def setParentMeta(self, puuid):
        meta = self.getMetadata()
        meta[sc.PUUID] = puuid
        self.setMetadata(meta)

    def setParentTag(self, puuid):
        # No LVM tag equivalent for native RBD volumes.
        pass

    def getParentMeta(self):
        return self.getParent()

    def getParentTag(self):
        return self.getParent()

    def getMetaSlot(self):
        raise se.UnsupportedOperation(
            "RBD volumes do not use LVM metadata slots")

    def llPrepare(self, rw=False, setrw=False):
        self.validateVolumePath()

    @classmethod
    def teardown(cls, sdUUID, volUUID, justme=False):
        # VM data disks are not locally mapped on the host.
        pass

    def _setrw(self, rw):
        # Not applicable to RBD network disks.
        pass

    def _share(self, dstImgPath):
        raise se.UnsupportedOperation("RBD MVP does not support volume share")

    def _extendSize(self, newSize):
        rbd_utils.resize_image(
            self.pool, self.rbd_image, newSize, ceph_user=self._ceph_user())
        meta = self.getMetadata()
        meta[sc.CAPACITY] = str(newSize)
        self.setMetadata(meta)

    def optimal_size(self):
        return self.getVolumeSize()

    def requires_create(self):
        return False

    def recheckIfLeaf(self):
        return True

    def getVmVolumeInfo(self):
        manifest = self._domain_manifest
        info = super(RBDVolumeManifest, self).getVmVolumeInfo()
        info.update({
            "type": self.DISK_TYPE,
            "path": self.getVolumePath(),
            "protocol": "rbd",
            "hosts": manifest.monitors(),
        })
        auth = manifest.libvirt_auth()
        if auth:
            info["auth"] = auth

        lease = manifest.getVolumeLease(self.imgUUID, self.volUUID)
        if lease.path is not None and lease.offset is not None:
            info["leasePath"] = lease.path
            info["leaseOffset"] = lease.offset
        return info

    @classmethod
    def getImageVolumes(cls, sdUUID, imgUUID):
        manifest = sdCache.produce_manifest(sdUUID)
        result = []
        for image in rbd_utils.list_volume_images(
                manifest.pool, ceph_user=manifest.ceph_user()):
            image_meta = rbd_utils.get_image_meta(
                manifest.pool,
                image,
                "imgUUID",
                ceph_user=manifest.ceph_user())
            if image_meta == imgUUID:
                result.append(image[len("volume-"):])
        return result

    @classmethod
    def newVolumeLease(cls, metaId, sdUUID, volUUID):
        # Native RBD volume leases are external leases in xleases.
        sdCache.produce(sdUUID).create_volume_lease(volUUID)


class RBDVolume(volume.Volume):
    manifestClass = RBDVolumeManifest

    @classmethod
    def create(cls, dom, imgUUID, capacity, volFormat, preallocate, diskType,
               volUUID, desc, srcImgUUID, srcVolUUID, initial_size=None,
               legal=True, sequence=0):
        if volFormat != sc.RAW_FORMAT:
            raise se.IncorrectFormat(sc.type2name(volFormat))
        if srcVolUUID != sd.BLANK_UUID:
            raise se.UnsupportedOperation(
                "RBD MVP does not support snapshots/clones")

        pool = dom.manifest.pool
        ceph_user = dom.manifest.ceph_user()
        image = rbd_utils.volume_image_name(volUUID)
        rbd_utils.create_image(pool, image, capacity, ceph_user=ceph_user)

        try:
            dom.create_volume_lease(volUUID)
        except Exception:
            log.error(
                "Rolling back RBD image %s/%s after lease failure",
                pool, image, exc_info=True)
            try:
                rbd_utils.remove_image(pool, image, ceph_user=ceph_user)
            finally:
                raise

        metadata = _new_metadata(
            dom.sdUUID,
            imgUUID,
            volUUID,
            capacity,
            preallocate,
            diskType,
            desc,
            legal,
            sequence,
        )
        rbd_utils.set_image_meta(
            pool, image, metadata, ceph_user=ceph_user)
        return cls(dom._getRepoPath(), dom.sdUUID, imgUUID, volUUID)

    def delete(self, postZero=False, force=False, discard=False):
        dom = sdCache.produce(self.sdUUID)
        ceph_user = dom.manifest.ceph_user()
        try:
            dom.delete_volume_lease(self.volUUID)
        except se.NoSuchLease:
            log.warning("RBD volume %s had no xlease", self.volUUID)
        rbd_utils.remove_image(
            self._manifest.pool,
            self._manifest.rbd_image,
            ceph_user=ceph_user)


def _pool_for_domain(sdUUID):
    manifest = sdCache.produce_manifest(sdUUID)
    return manifest.pool


def _new_metadata(sdUUID, imgUUID, volUUID, capacity, preallocate, diskType,
                  desc, legal, sequence):
    now = int(time.time())
    return {
        sc.CAPACITY: str(capacity),
        sc.TYPE: sc.type2name(preallocate),
        sc.FORMAT: sc.type2name(sc.RAW_FORMAT),
        sc.DISKTYPE: diskType or sc.DATA_DISKTYPE,
        sc.VOLTYPE: sc.type2name(sc.LEAF_VOL),
        sc.PUUID: sd.BLANK_UUID,
        sc.DOMAIN: sdUUID,
        sc.CTIME: str(now),
        sc.IMAGE: imgUUID,
        sc.DESCRIPTION: desc or "",
        sc.LEGALITY: sc.LEGAL_VOL if legal else sc.ILLEGAL_VOL,
        sc.MTIME: "0",
        sc.GENERATION: str(sc.DEFAULT_GENERATION),
        sc.SEQUENCE: str(sequence),
        "volUUID": volUUID,
    }

