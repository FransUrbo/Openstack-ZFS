Openstack-ZFS
=============

zfs plugin for Cinder in Openstack Mitaka.

This version _require_ that ZoL have been compiled with my
'shareiscsi' patch - https://github.com/zfsonlinux/zfs/pull/1099.

This pull request is closed, because "the powers that be" have
had no interest in it. If enough people keep asking them for it,
it will maybe (!!) happen.

Untill then (or until 'they' write this support into ZED which,
to be fair, is a much better idea), I try to keep my own branch
as up to date as I can. HOWEVER (!!) I only push to that when
I upgrade myself, which isn't that often..

  https://github.com/fransurbo/zfs/tree/iscsi

# History

Based off of work by tparker00. https://github.com/tparker00/Openstack-ZFS

Based off of work from David Douard in the following blog post. http://www.logilab.org/blogentry/114769

# Install

To install copy zol.py to /usr/lib/python2.7/dist-packages/cinder/volume/drivers and add the following to /etc/cinder/cinder.conf

```
# ZFS/ZoL driver - https://github.com/FransUrbo/Openstack-ZFS  
[zol]
volume_driver = cinder.volume.drivers.zol.ZFSonLinuxISCSIDriver  
volume_group = <zvol_path>  
iscsi_ip_prefix = <ip_prefix>  
iscsi_ip_address = <cinder_ip>  
san_thin_provision = <true|false>  
san_ip = $my_ip
san_zfs_volume_base = <zvol_path>  
san_is_local = <true|false>  
use_cow_images = <true|false>  
san_login = <san_admin_user>  
san_private_key = <ssh_key_path>  
san_zfs_command = <path_to_zfs_or_wrapper_on_san>  
verbose = true  
```

/etc/cinder/rootwrap.d/volume.filters needs the following line added as well  

```
zfs: CommandFilter, /sbin/zfs, root  
```

You will also need to create a volume type for this

```
openstack volume type create --description "ZFS volumes" --public zfs  
openstack volume type set --property volume_backend_name=ZOL zfs  
```
