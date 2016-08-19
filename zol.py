# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2016 Turbo Fredriksson <turbo@bayour.com>
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

This is a fork from https://github.com/tparker00/Openstack-ZFS by tparker00
with modifications to make it work well with Cinder and OpenStack Mitaka.

My setup is utilizing remotly stored ZFS volumes so local access was not tested.
"""

import os
import socket

from oslo_concurrency import processutils
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
from cinder.image import image_utils

LOG = logging.getLogger(__name__)

san_opts = [
    cfg.StrOpt('san_zfs_volume_base',
               # TODO: Make this a list.
               default='cinder',
               help='Filesystem base where new ZFS volumes will be created.'),
    cfg.StrOpt('san_zfs_command',
               default='zfs',
               help='The ZFS command.'),
    cfg.StrOpt('san_zpool_command',
               default='zpool',
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

    def create_volume(self, volume):
        zfs_poolname = self._build_zfs_poolname(volume['name'])

        # Create a zfs volume
        cmd = [CONF.san_zfs_command, 'create']
        if CONF.san_thin_provision:
            cmd.append('-s')
        cmd.extend(['-V%sg' % volume['size']])
        if self._stats['pools'][0]['encryption_support']:
            cmd.extend(['-o', 'encryption='+CONF.san_zfs_encryption])
        cmd.extend(['-o', 'compression='+CONF.san_zfs_compression])
        cmd.extend(['-o', 'dedup='+CONF.san_zfs_dedup])
        cmd.extend(['-o', 'volblocksize='+str(CONF.san_zfs_blocksize)])
        cmd.extend(['-o', 'checksum='+CONF.san_zfs_checksum])
        cmd.extend(['-o', 'copies='+CONF.san_zfs_copies])
        cmd.extend(['-o', 'sync='+CONF.san_zfs_sync])
        cmd.append(zfs_poolname)

        LOG.debug('About to run command: "%s"', *cmd)
        self._execute(*cmd, run_as_root=True)

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

        # CMD: zpool get -Hp size share
        total_capacity = self._execute(CONF.san_zpool_command,
                                       'get', '-Hp', 'size',
                                       volgrp, run_as_root=True)
        if total_capacity:
            total_capacity = int(total_capacity[0].split( )[2])
        else:
            total_capacity = 0
            
        # CMD: zfs get -Hpovalue available share/VirtualMachines/Blade_Center
        free_capacity = self._execute(CONF.san_zfs_command,
                                      'get', '-Hpovalue', 'available',
                                      self.configuration.san_zfs_volume_base,
                                      run_as_root=True)
        if free_capacity:
            free_capacity = int(free_capacity[0])
        else:
            free_capacity = 0

        # CMD: zpool get -Hp feature@encryption share
        feature = self._execute(CONF.san_zpool_command,
                                'get', '-Hp', 'feature@encryption',
                                volgrp, run_as_root=True)
        if feature[0]:
            feature_val = feature[0].split( )[2]
            if feature_val == 'enabled':
                supports_encryption = True
            else:
                supports_encryption = False
        else:
            supports_encryption = False

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
            multiattach=False,
            encryption_support=supports_encryption
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

    def extend_volume(self, volume, new_size):
        """Extend an existing volume's size."""
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        try:
            out, err = self._execute(CONF.san_zfs_command, 'set',
                                     'volsize=' + self._sizestr(new_size), 
                                     zfs_poolname, run_as_root=True)
        except Exception as e:
            return False
        return True

    def _rename_volume(self, old_name, new_name):
        # See if this target is logged in.
        target = self._get_iscsi_sessions(old_name)
        if target:
            # Yes. Logout the target.
            if not self._logout_target(self.configuration.san_ip + ':' +
                                       str(self.configuration.iscsi_port),
                                       target):
                LOG.error(_LE('Cannot logout iSCSI sessions, cannot rename volume'))
                return False

        # Rename volume.
        try:
            self._execute(CONF.san_zfs_command, 'rename',
                          old_name, new_name, zfs_poolname,
                          run_as_root=True)
        except putils.ProcessExecutionError:
            with excutils.save_and_reraise_exception():
                LOG.exception(_LE('Error renaming volume'))
                                            
    def manage_existing(self, volume, existing_ref):
        """Manages an existing volume.

        Renames the volume to match the expected name for the volume.
        Error checking done by manage_existing_get_size is not repeated.
        """
        LOG.debug('manage_existing: volume=%s', volume)
        LOG.debug('manage_existing: existing_ref=%s', existing_ref)

        if self._volume_not_present(volume['name']):
            # If the volume isn't present, then don't attempt to delete
            LOG.debug("VOLUME NOT FOUND (%s)" % (volume['name']))
            return True

        vol_src = self._build_zfs_poolname(existing_ref['source-name'])
        if volutils.check_already_managed_volume(vol_src):
            raise exception.ManageExistingAlreadyManaged(volume_ref=vol_src)

        # Attempt to rename the volume to match the OpenStack internal name.
        vol_dst = self._build_zfs_poolname(volume['name'])
        try:
            self._rename_volume(vol_src, vol_dst)
        except putils.ProcessExecutionError as exc:
            exception_message = (_("Failed to rename volume %(name)s, "
                                   "error message was: %(err_msg)s")
                                 % {'name': vol_src,
                                    'err_msg': exc.stderr})
            raise exception.VolumeBackendAPIException(data=exception_message)
                                
    def unmanage(self, volume):
        # TODO
        pass

    def _volume_not_present(self, volume_name):
        zfs_poolname = self._build_zfs_poolname(volume_name)
        LOG.debug("_volume_not_present(%s): %s" % (volume_name, zfs_poolname))

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
        LOG.debug('create_volume_from_snapshot: volume=%s', volume)
        LOG.debug('create_volume_from_snapshot: snapshot=%s', snapshot)

        vol = 'volume-'+snapshot['volume_id']+'@snapshot-'+snapshot['id']
        zfs_snap = self._build_zfs_poolname(vol)
        zfs_vol = self._build_zfs_poolname('volume-'+volume['id'])
        LOG.debug('create_volume_from_snapshot: zfs_snap=%s, zfs_vol=%s',
                  zfs_snap, zfs_vol)
        
        self._execute(CONF.san_zfs_command, 'clone', zfs_snap,
                      zfs_vol, run_as_root=True)
        self._execute(CONF.san_zfs_command, 'promote', zfs_vol, run_as_root=True)

    def delete_volume(self, volume):
        """Deletes a volume."""
        self.terminate_connection(volume, False)

        # Destroy the volume.
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        if self._execute(CONF.san_zfs_command, 'destroy', zfs_poolname,
                             run_as_root=True):
            LOG.debug('Delete volume successful')
            return True
        else:
            LOG.error(_LE('Cannot delete volume'))
            return False

    def _find_target(self, volume_id):
        """Get the iSCSI target for the volume.

        Similar to iscsi:ISCSITarget:_do_iscsi_discovery()
        However, that uses the Cinder hostname to get targets,
        but since I'm using a remote SAN ('san_ip'), I need to
        discover on that.
        """
        try:
            (out, _err) = utils.execute('iscsiadm', '-m', 'discovery',
                                        '-t', 'sendtargets',
                                        '-p', self.configuration.san_ip,
                                        run_as_root=True)
            LOG.debug('_find_target: out=%s (%s)', out, _err)
        except processutils.ProcessExecutionError as ex:
            LOG.error(_LE("ISCSI discovery attempt failed for: %s") %
                      self.configuration.san_ip)
            LOG.debug(("Error from iscsiadm -m discovery: %s") % ex.stderr)
            return False

        # Find the IQN of the volume.
        # The 'shareiscsi' replaces all dashes with dots,
        # and we're only interested in the actual IQN..
        for entry in out.splitlines():
            if 'volume.'+volume_id.replace('-', '.') in entry.split( )[1]:
                return entry.split( )[1]

        return False

    def _login_target(self, portal, target):
        """Login to a target"""
        LOG.debug('_login_target(%s, %s)', portal, target)

        try:
            (out, _err) = utils.execute('iscsiadm', '-m', 'node',
                                        '-p', portal, '-T', target, '-l',
                                        run_as_root=True)
            LOG.debug('_login_target: out=%s (%s)', out, _err)
        except processutils.ProcessExecutionError as ex:
            LOG.error(_LE("ISCSI login attempt failed for: %s:%s") %
                      portal, target)
            LOG.debug(("Error from iscsiadm -m node: %s") % ex.stderr)
            return False

        # Find out if we have the words 'Login to .* successful' in the message
        for entry in out.splitlines():
            if 'successful' in entry.split( )[1]:
                return True

        return False

    def _logout_target(self, portal, target):
        """Logout a target"""
        LOG.debug('_logout_target(%s, %s)', portal, target)

        try:
            (out, _err) = utils.execute('iscsiadm', '-m', 'node',
                                        '-p', portal, '-T', target, '-u',
                                        run_as_root=True)
        except processutils.ProcessExecutionError as ex:
            LOG.debug(("Error from iscsiadm -m node: %s") % ex.stderr)
            LOG.error(_LE("ISCSI logout attempt failed for: %s:%s") %
                      (portal, target))
            return False

        # Find out if we have the words 'Logout to .* successful' in the message
        for entry in out.splitlines():
            LOG.debug('_logout_target: CHECK: successful in %s', entry)
            if 'successful' in entry:
                return True

        return False

    def _get_iscsi_sessions(self, volume_id):
        """See if we have a target logged in"""
        LOG.debug('_get_iscsi_sessions(%s)', volume_id)

        try:
            (out, _err) = utils.execute('iscsiadm', '-m', 'session',
                                        run_as_root=True)
            LOG.debug('_get_iscsi_sessions: out=%s (%s)', out, _err)
        except processutils.ProcessExecutionError as ex:
            LOG.debug(("Error from iscsiadm -m session: %s") % ex.stderr)
            return False

        # Is the target logged in?
        for entry in out.splitlines():
            if 'volume.'+volume_id.replace('-', '.') in entry.split( )[3]:
                LOG.debug('_get_iscsi_sessions: entry=%s', entry)
                return entry.split( )[3]

        return False

    def _find_iscsi_block_device(self, volume_id):
        """Find the block device for this logged in iSCSI target"""
        LOG.debug('_find_iscsi_block_device(%s)', volume_id)

        target = self._find_target(volume_id)
        if not target:
            LOG.error(_LE("ISCSI find block device failed for: %s") %
                      volume_id)
            return False

        LOG.debug('_find_iscsi_block_device: target=%s', target)

        try:
            (out, _err) = utils.execute('find', '/dev/disk/by-path', '-name',
                                        '*' + target + '*',
                                        run_as_root=True)
            LOG.debug('_find_iscsi_block_device: out=%s (%s)', out, _err)
            return out
        except processutils.ProcessExecutionError as ex:
            LOG.debug(("Error from find /dev/disk/by-path: %s") % ex.stderr)
            return False

    def initialize_connection(self, volume, connector=None):
        """Initializes the connection and returns connection info."""
        # Find the target/iqn.
        target = self._find_target(volume['name_id'])
        if not target:
            LOG.error(_LE("ISCSI init connection failed for: %s") %
                      volume['name_id'])
            return False

        LOG.debug('initialize_connection: target=%s', target)

        # Login to the target.
        if self._login_target(self.configuration.san_ip + ':' +
                               str(self.configuration.iscsi_port),
                               target):
            LOG.error(_LE("ISCSI login failed for: %s") %
                      volume['name_id'])
            return False

        block_dev = self._find_iscsi_block_device(volume['name_id'])
        LOG.debug('initialize_connection: block_dev=%s', block_dev)

        return {
            'driver_volume_type': self.configuration.iscsi_protocol,
            'data': {
                'target_discovered': True,
                'target_portal': self.configuration.san_ip + ':' + str(self.configuration.iscsi_port),
                'target_iqn': target,
                'volume_id': volume['name_id'],
                'volume_path': block_dev,
                'discard': False,
            }
        }

    def terminate_connection(self, volume, connector, **kwargs):
        """Terminate the connection."""
        if self._volume_not_present(volume['name']):
            # If the volume isn't present, then don't attempt to disconnect.
            LOG.debug("VOLUME NOT FOUND (%s)" % (volume['name']))
            return True

        # Find the target/iqn.
        target = self._find_target(volume['name_id'])
        if not target:
            LOG.error(_LE("ISCSI term connection failed for: %s") %
                      volume['name_id'])
            return False

        LOG.debug('terminate_connection: target=%s', target)

        target = self._get_iscsi_sessions(target)
        if target:
            # Yes - Logout the target.
            if not self._logout_target(self.configuration.san_ip + ':' +
                                       str(self.configuration.iscsi_port),
                                       target):
                LOG.error(_LE("ISCSI logout failed for: %s") %
                          volume['name_id'])
                return False

        return True
        
    def create_export(self, context, volume, connector=None):
        """Creates an export for a logical volume."""
        zfs_poolname = self._build_zfs_poolname(volume['name'])
        LOG.debug('create_export(): Trying to share "%s"', zfs_poolname)
        
        # zfs doesn't return anything valuable.
        self._execute(CONF.san_zfs_command, 'set', 'shareiscsi=on',
                      zfs_poolname, run_as_root=True)
        
        #model_update['provider_location'] = _iscsi_location(
        #    CONF.iscsi_ip_address, tid, iscsi_name, lun)
        #return model_update

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

    def copy_image_to_volume(self, context, volume, image_service, image_id):
        """Fetch the image from image_service and write it to the volume."""
        image_utils.fetch_to_raw(context,
                                 image_service,
                                 image_id,
                                 self._find_iscsi_block_device(volume['name_id']),
                                 self.configuration.volume_dd_blocksize,
                                 size=volume['size'])

    def copy_volume_to_image(self, context, volume, image_service, image_meta):
        """Copy the volume to the specified image."""
        image_utils.upload_volume(context,
                                  image_service,
                                  image_meta,
                                  self._find_iscsi_block_device(volume['name_id']))

    def local_path(self, volume):
        return '/dev/zvol/%s' % self._build_zfs_poolname(volume['name'])

    def _build_zfs_poolname(self, volume_name):
        return '%s/%s' % (self.configuration.san_zfs_volume_base, volume_name)
