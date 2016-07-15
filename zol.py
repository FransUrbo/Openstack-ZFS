# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# Copyright 2011 Justin Santa Barbara
# Copyright 2012 David DOUARD, LOGILAB S.A.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Driver for ZFS-on-Linux-stored volumes.

This is mainly taken from  http://www.logilab.org/blogentry/114769 with
modifications to make it work well with cinder and OpenStack Folsom.

My setup is utilizing locally stored ZFS volumes so SSH access was not tested
"""

import os
import socket

from oslo_config import cfg
from oslo_utils import importutils
from oslo_log import log as logging

from cinder import exception
from cinder import objects
from cinder import utils
from cinder.i18n import _, _LE, _LI
from cinder.volume import driver
from cinder.volume.targets import iscsi
from cinder.volume.drivers.san import san

LOG = logging.getLogger(__name__)

san_opts = [
    cfg.StrOpt('san_zfs_volume_base',
               # TODO: Make this a list.
               default='cinder',
               help='Filesystem base where new ZFS volumes will be created.'),
    cfg.StrOpt('san_zfs_command',
               default='/sbin/zfs',
               help='The ZFS command.'),
    cfg.StrOpt('san_zpool_command',
               default='/sbin/zpool',
               help='The ZPOOL command.'),
    cfg.FloatOpt('zol_max_over_subscription_ratio',
                 # This option exists to provide a default value for the
                 # ZOL driver which is different than the global default.
                 default=1.0,
                 help='max_over_subscription_ratio setting for the ZOL '
                      'driver.  If set, this takes precedence over the '
                      'general max_over_subscription_ratio option.  If '
                      'None, the general option is used.'),
    cfg.StrOpt('san_zfs_compression',
               default='on',
               choices=['on', 'off', 'gzip', 'gzip-1', 'gzip-2', 'gzip-3',
                        'gzip-4', 'gzip-5', 'gzip-6', 'gzip-7', 'gzip-8',
                        'gzip-9', 'lzjb', 'zle', 'lz4'],
               help='Compression value for new ZFS volumes.'),
    cfg.StrOpt('san_zfs_dedup',
               default='off',
               choices=['on', 'off', 'sha256', 'verify', 'sha256, verify'],
               help='Deduplication value for new ZFS volumes.'),
    cfg.IntOpt('san_zfs_blocksize',
               default=4096,
               help='Block size for datasets.'),
    cfg.StrOpt('san_zfs_checksum',
               default='on',
               choices=['on', 'off', 'fletcher2', 'fletcher4', 'sha256'],
               help='Checksum value for new ZFS volumes.'),
    cfg.StrOpt('san_zfs_copies',
               default='1',
               choices=['1', '2', '3'],
               help='Number of data copies for new ZFS volumes.'),
    cfg.StrOpt('san_zfs_sync',
               default='standard',
               choices=['standard', 'always', 'disabled'],
               help='Behaviour of synchronous requests for new ZFS volumes.'),
    cfg.StrOpt('san_zfs_encryption',
               default='off',
               choices=['on', 'off', 'aes-128-ccm', 'aes-192-ccm', 'aes-256-ccm',
                        'aes-128-gcm', 'aes-192-gcm', 'aes-256-gcm'],
               help='Encryption value for new ZFS volumes.')
]

CONF = cfg.CONF
CONF.register_opts(san_opts)


class ZFSonLinuxISCSIDriver(san.SanISCSIDriver):
    """Executes commands relating to ZFS-on-Linux-hosted ISCSI volumes.

    Basic setup for a ZoL iSCSI server:

    XXX

    Note that current implementation of ZFS on Linux does not handle:

      zfs allow/unallow

    For now, needs to have root access to the ZFS host. The best is to
    use a ssh key with ssh authorized_keys restriction mechanisms to
    limit root access.

    Make sure you can login using san_login & san_password/san_private_key
    """
    VERSION = '2.0.0'

    _local_execute = utils.execute

    def _getrl(self):
        return self._runlocal
    
    def _setrl(self, v):
        if isinstance(v, basestring):
            v = v.lower() in ('true', 't', '1', 'y', 'yes')
        self._runlocal = v
    run_local = property(_getrl, _setrl)

    def _sizestr(self, size_in_g):
        return '%sG' % size_in_g

    def __init__(self, *args, **kwargs):
        super(ZFSonLinuxISCSIDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(san_opts)
        self.hostname = socket.gethostname()
        self.backend_name =\
            self.configuration.safe_get('volume_backend_name') or 'ZOL'

        # Target Driver is what handles data-transport
        # Transport specific code should NOT be in
        # the driver (control path), this way
        # different target drivers can be added (iscsi, FC etc)
        target_driver = \
            self.target_mapping[self.configuration.safe_get('iscsi_helper')]

        LOG.debug('Attempting to initialize ZOL driver with the '
                  'following target_driver: %s', target_driver)

        self.target_driver = importutils.import_object(
            target_driver,
            configuration=self.configuration,
            db=self.db,
            executor=self._execute)
        self.protocol = self.target_driver.protocol

        self._sparse_copy_volume = False

        if self.configuration.zol_max_over_subscription_ratio is not None:
            self.configuration.max_over_subscription_ratio = \
                self.configuration.zol_max_over_subscription_ratio

        LOG.info("run local = %s (%s)" % (self.run_local, CONF.san_is_local))

    def set_execute(self, execute):
        LOG.debug("override local execute cmd with %s (%s)" % (
            repr(execute), execute.__module__))
        self._local_execute = execute

    def _execute(self, *cmd, **kwargs):
        if self.run_local:
            LOG.debug("LOCAL execute cmd: %s %s" % (cmd, kwargs))
            return self._local_execute(*cmd, **kwargs)
        else:
            LOG.debug("SSH execute cmd: %s %s" % (cmd, kwargs))
            check_exit_code = kwargs.pop('check_exit_code', None)
            command = ' '.join(cmd)
            return self._run_ssh(command, check_exit_code)

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
	zfs_poolname = self._build_zfs_poolname(snapshot['volume_name'])
        snap_path = "%s@%s" % (zfs_poolname, snapshot['name'])
        self._execute(CONF.san_zfs_command, 'snapshot', snap_path,
                                    run_as_root=True)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
	zfs_poolname = self._build_zfs_poolname(snapshot['volume_name'])
	snap_path  = "%s@%s" % (zfs_poolname, snapshot['name'])
        if self._volume_not_present(snapshot['volume_name']):
            # If the snapshot isn't present, then don't attempt to delete
	    LOG.debug("SNAPSHOT NOT FOUND %s",(snap_path))
            return True
        self._execute(CONF.san_zfs_command, 'destroy', snap_path,
                                    run_as_root=True)

    def _create_volume(self, volume_name, sizestr):
        zfs_poolname = self._build_zfs_poolname(volume_name)

        # Create a zfs volume
        cmd = [CONF.san_zfs_command, 'create']
        if CONF.san_thin_provision:
            cmd.append('-s')
        cmd.extend(['-V', sizestr])
        #cmd.extend(['-o', 'encryption='+CONF.san_zfs_encryption])
        cmd.extend(['-o', 'compression='+CONF.san_zfs_compression])
        cmd.extend(['-o', 'dedup='+CONF.san_zfs_dedup])
        cmd.extend(['-o', 'blocksize='+str(CONF.san_zfs_blocksize)])
        cmd.extend(['-o', 'checksum='+CONF.san_zfs_checksum])
        cmd.extend(['-o', 'copies='+CONF.san_zfs_copies])
        cmd.extend(['-o', 'sync='+CONF.san_zfs_sync])
        cmd.append(zfs_poolname)
        LOG.debug('About to run command: "%s"', *cmd)
        self._execute(*cmd, run_as_root=True)

    def create_volume(self, volume):
        self._create_volume(volume['name'], self._sizestr(volume['size']))

    def _update_volume_stats(self):
        """Retrieve stats info from volume group."""

        LOG.debug("Updating volume stats")

        # XXX FIXME support multiple pools (?)
        #           Should be possible to do this in a loop over "san_zfs_volume_base".
        volgrp = self.configuration.san_zfs_volume_base.split('/')[0]

        data = {}

        # Note(zhiteng): These information are driver/backend specific,
        # each driver may define these values in its own config options
        # or fetch from driver specific configuration file.
        data["volume_backend_name"] = self.backend_name
        data["vendor_name"] = 'Open Source'
        data["driver_version"] = self.VERSION
        data["storage_protocol"] = self.protocol
        data["pools"] = []

        # zpool get -Hp size share
        total_capacity = self._execute(CONF.san_zpool_command,
                                       'get', '-Hp', 'size',
                                       volgrp, run_as_root=True)
        if total_capacity:
            total_capacity = int(total_capacity[0].split( )[2])
        else:
            total_capacity = 0
            
        # zfs get -Hpovalue available share/VirtualMachines/Blade_Center
        free_capacity = self._execute(CONF.san_zfs_command,
                                      'get', '-Hpovalue', 'available',
                                      self.configuration.san_zfs_volume_base,
                                      run_as_root=True)
        if free_capacity:
            free_capacity = int(free_capacity[0])
        else:
            free_capacity = 0

        provisioned_capacity = round(total_capacity - free_capacity, 2)

        location_info = "ZFSonLinuxISCSIDriver:%s" % self.hostname

        thin_enabled = self.configuration.san_thin_provision == 'true'

        # Calculate the total volumes used by the VG group.
        # This includes volumes and snapshots.
        total_volumes = self._execute(CONF.san_zfs_command,
                                      'list', '-Hroname',
                                      self.configuration.san_zfs_volume_base,
                                      run_as_root=True)
        if total_volumes:
            # We get the base fs here, which isn't a volume.
            total_volumes = len(total_volumes[0]) - 1
        else:
            total_volumes = 0

        # Skip enabled_pools setting, treat the whole backend as one pool
        # XXX FIXME support multiple pools (?)
        #           Should be possible to do this in a loop over "san_zfs_volume_base".
        # allocated_capacity_gb=0 (?)
        single_pool = {}
        single_pool.update(dict(
            pool_name=volgrp,
            total_capacity_gb=int(total_capacity / 1024 / 1024 / 1024),
            free_capacity_gb=int(free_capacity / 1024 / 1024 / 1024),
            provisioned_capacity_gb=int(provisioned_capacity / 1024 / 1024 / 1024),
            reserved_percentage=self.configuration.reserved_percentage,
            location_info=location_info,
            QoS_support=False,
            max_over_subscription_ratio=(
                self.configuration.max_over_subscription_ratio),
            thin_provisioning_support=thin_enabled,
            thick_provisioning_support=not thin_enabled,
            total_volumes=total_volumes,
            filter_function=self.get_filter_function(),
            goodness_function=self.get_goodness_function(),
            multiattach=False
        ))
        data["pools"].append(single_pool)

        # Check availability of sparse volume copy.
        data['sparse_copy_volume'] = self._sparse_copy_volume

        self._stats = data

    def get_volume_stats(self, refresh=False):
        """Get volume status.

        If 'refresh' is True, run update the stats first.
        """

        if refresh:
            self._update_volume_stats()

        return self._stats

    def _volume_not_present(self, volume_name):
        zfs_poolname = self._build_zfs_poolname(volume_name)
	LOG.debug("ZFS_POOLNAME (%s)" % (zfs_poolname))

        try:
            out, err = self._execute(CONF.san_zfs_command, 'list', '-H', 
                                     zfs_poolname, run_as_root=True)
            if out.startswith(zfs_poolname):
                return False
        except Exception as e:
            # If the volume isn't present
            return True
        return False

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        zfs_snap = self._build_zfs_poolname(snapshot['name'])
        zfs_vol = self._build_zfs_poolname(volume['name'])

        self._execute(CONF.san_zfs_command, 'clone', zfs_snap,
                      zfs_vol, run_as_root=True)
        self._execute(CONF.san_zfs_command, 'promote', zfs_vol, run_as_root=True)

    def delete_volume(self, volume):
        """Deletes a volume."""
        if self._volume_not_present(volume['name']):
            # If the volume isn't present, then don't attempt to delete
	    LOG.debug("VOLUME NOT FOUND (%s)" % (volume['name']))
            return True
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        self._execute(CONF.san_zfs_command, 'destroy', zfs_poolname, run_as_root=True)

    def create_export(self, context, volume, connector=None):
        """Creates an export for a logical volume."""
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        LOG.debug('create_export(): Trying to share "%s"', zfs_poolname)
        
        # zfs doesn't return anything valuable.
        self._execute(CONF.san_zfs_command, 'set', 'shareiscsi=on',
                      zfs_poolname, run_as_root=True)
        
        model_update['provider_location'] = _iscsi_location(
            CONF.iscsi_ip_address, tid, iscsi_name, lun)
        return model_update

    def remove_export(self, context, volume):
        """Removes an export for a logical volume."""
        zfs_poolname = self._build_zfs_poolname(volume['name'])

        # zfs doesn't return anything valuable.
        self._execute(CONF.san_zfs_command, 'set', 'shareiscsi=off',
                      zfs_poolname, run_as_root=True)

    def check_for_export(self, context, volume_id):
        """Make sure volume is exported."""
        vol_uuid_file = 'volume-%s' % volume_id
        volume_path = os.path.join(CONF.volumes_dir, vol_uuid_file)
        if os.path.isfile(volume_path):
            iqn = '%s%s' % (CONF.iscsi_target_prefix,
                            vol_uuid_file)
        else:
            raise exception.PersistentVolumeFileNotFound(volume_id=volume_id)

        # TODO(jdg): In the future move all of the dependent stuff into the
        # cooresponding target admin class
        if not isinstance(self.tgtadm, iscsi.TgtAdm):
            tid = self.db.volume_get_iscsi_target_num(context, volume_id)
        else:
            tid = 0

        try:
            self.tgtadm.show_target(tid, iqn=iqn)
        except exception.ProcessExecutionError, e:
            # Instances remount read-only in this case.
            # /etc/init.d/iscsitarget restart and rebooting cinder-volume
            # is better since ensure_export() works at boot time.
            LOG.error(_("Cannot confirm exported volume "
                        "id:%(volume_id)s.") % locals())
            raise

    def local_path(self, volume):
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        zvoldev = '/dev/zvol/%s' % zfs_poolname
        return zvoldev

    def _build_zfs_poolname(self, volume_name):
        zfs_poolname = '%s/%s' % (self.configuration.san_zfs_volume_base,
                                 volume_name)
        return zfs_poolname
