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

Create a host aggregate:

```
openstack aggregate create --zone nova --property volume_backend_name=ZOL zfs
```

You will also need to create a volume type for this

```
openstack volume type create --description "ZFS volumes" --public zfs  
openstack volume type set --property volume_backend_name=ZOL zfs  
```

To make sure you have a flavor that prefers ZOL as backend, create
some flavors:

```
openstack flavor create --ram   512 --disk  2 --vcpus 1 --disk  5 z1.nano
openstack flavor create --ram  1024 --disk 10 --vcpus 1 --disk  5 z1.tiny
openstack flavor create --ram  2048 --disk 20 --vcpus 1 --disk 10 z1.small
openstack flavor create --ram  4096 --disk 40 --vcpus 1           z1.medium
openstack flavor create --ram  8192 --disk 40 --vcpus 1           z1.large
openstack flavor create --ram 16384 --disk 40 --vcpus 1           z1.xlarge
# [etc]

openstack flavor list --all --column Name --format csv --quote none | \
    grep ^z | \
    while read flavor; do
        openstack flavor set --property volume_backend_name=ZOL "${flavor}"
    done
```

# Security

Even though ZoL now have support for allow/unallow in its master branch,
I have not yet upgraded so can there for not comment on that part. I
run my zfs/zpool commands as the root user, but to improve the security
somewhat, I've restricted what the sshkey (see the "san_private_key"
option above) can do.

So in the /root/.ssh/authorized_keys file, I have the following:

```
from="192.168.69.1",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding,command="/root/bin/zfswrapper $SSH_ORIGINAL_COMMAND" ssh-rsa AAAAB... user@host
```

This makes sure that the key specified, comming from 192.168.69.1 (which
is my internal router) can only run the "/root/bin/zfswrapper"
command.

This shell script looks like this (and is located on the ZoL SAN host):

```
#!/bin/sh

# https://www.logilab.org/blogentry/114769
# http://larstobi.blogspot.co.uk/2011/01/restrict-ssh-access-to-one-command-but.html
echo "[$(date)] ${SSH_ORIGINAL_COMMAND}" >> /root/.zfsopenstack.log

CMD=$(echo ${SSH_ORIGINAL_COMMAND} | awk '{print $1}')
if [ "${CMD}" != "zfs" -a \
     "${CMD}" != "tgtadm" -a \
     "${CMD}" != "zpool" ]
then
    echo "Can do only zfs/tgtadm stuff here"
    exit 1
fi

exec ${SSH_ORIGINAL_COMMAND}
```
