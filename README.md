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

\# ZFS/ZoL driver - https://github.com/FransUrbo/Openstack-ZFS  
[zol]
volume\_driver = cinder.volume.drivers.zol.ZFSonLinuxISCSIDriver  
volume\_group = \<zvol\_path\>  
iscsi\_ip\_prefix = \<ip\_prefix\>  
iscsi\_ip\_address = \<cinder\_ip\>  
san\_thin\_provision = \<true|false\>  
san\_ip = $my_ip
san\_zfs\_volume\_base = \<zvol\_path\>  
san\_is\_local = \<true|false\>  
use\_cow\_images = \<true|false\>  
san_login = \<san_admin_user\>  
san_private_key = \<ssh_key_path\>  
san_zfs_command = \<path_to_zfs_or_wrapper_on_san\>  
verbose = true  

/etc/cinder/rootwrap.d/volume.filters needs the following line added as well  
zfs: CommandFilter, /sbin/zfs, root

