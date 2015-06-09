
from __future__ import print_function

import os
import random
import re
import string
import sys

from fabric.api import env, put, sudo, task

valid_gpus = ['nvidia', 'nouveau', 'amd', 'intel', 'vbox', 'vmware']
base_packages = [
    'base', 'btrfs-progs', 'cronie', 'git', 'gptfdisk', 'networkmanager', 'nfs-utils',
    'pkgfile', 'puppet3', 'openssh', 'rsync', 'vim', 'zsh']
base_services = ['cronie', 'puppet', 'sshd']
gui_packages = [
    'aspell-en', 'gdm', 'gnome', 'gnome-extra', 'terminator', 'ttf-dejavu']
gui_services = ['gdm']


def generate_password(length):
    lst = [random.choice(string.ascii_letters + string.digits)
           for n in xrange(length)]
    return "".join(lst)


def pacstrap(packages):
    """
    Accepts a list of packages to be installed to env.dest.
    """
    script = """#!/bin/bash
count=0
while [[ $count -lt 5 ]]
do
    pacstrap -c "%s" %s | tee /tmp/out
    if grep 'invalid or corrupted package' /tmp/out
    then
        count=$((count+1))
        echo "Failed $count times!"
    else
        break
    fi
done
EOF""" % (env.dest, ' '.join(packages))
    sudo("cat <<-'EOF' > /tmp/pacstrap.sh\n" + script, quiet=True)
    sudo('chmod +x /tmp/pacstrap.sh', quiet=True)
    sudo('/tmp/pacstrap.sh', quiet=env.quiet)


def enable_multilib_repo():
    if not sudo("grep -q '^\[multilib\]' /etc/pacman.conf", warn_only=True).succeeded:
        sudo('echo [multilib] >> /etc/pacman.conf')
        sudo('echo Include = /etc/pacman.d/mirrorlist >> /etc/pacman.conf')


def enable_dray_repo():
    sudo('curl -o /tmp/repo.pkg.tar.xz https://repo.dray.be/dray-repo-latest')
    sudo('pacman -U --noconfirm /tmp/repo.pkg.tar.xz')


def enable_mdns():
    sudo('pacman -Sy --noconfirm avahi nss-mdns')
    sudo("sed -i 's/^hosts.*/hosts: files mdns_minimal [NOTFOUND=return] dns myhostname/' /etc/nsswitch.conf")
    sudo('nscd -i hosts', warn_only=True, quiet=True)


def gpu_install(gpu):
    if gpu == 'nvidia':
        gpu_packages = ['lib32-mesa', 'lib32-nvidia-libgl', 'nvidia']
    if gpu == 'nouveau':
        gpu_packages = ['lib32-mesa', 'xf86-video-nouveau']
        sudo("""sed -i '/MODULES=/s/"$/ nouveau"/' %s/etc/mkinitcpio.conf"""
             % env.dest)
    if gpu == 'amd':
        gpu_packages = ['lib32-mesa', 'xf86-video-ati', 'mesa-libgl', 'lib32-mesa-libgl', 'mesa-vdpau', 'lib32-mesa-vdpau']
    if gpu == 'intel':
        gpu_packages = ['lib32-mesa', 'xf86-video-intel']
    if gpu == 'vbox':
        gpu_packages = ['virtualbox-guest-utils']
        sudo("echo -e 'vboxguest\nvboxsf\nvboxvideo' >"
             "'%s/etc/modules-load.d/virtualbox.conf'" % env.dest)
    if gpu == 'vmware':
        gpu_packages = ['open-vm-tools', 'xf86-input-vmmouse', 'xf86-video-vmware']
        sudo("""sed -i 's/MODULES="/MODULES="vmhgfs /' %s/etc/mkinitcpio.conf""" % env.dest)
        sudo("echo 'cat /proc/version > /etc/arch-release' > %s/etc/cron.daily/vmware-version-update" % env.dest)
        sudo("chmod +x %s/etc/cron.daily/vmware-version-update" % env.dest)

    pacstrap(gpu_packages)

    if gpu == 'vmware':
        enable_services(['vmtoolsd', 'vmware-vmblock-fuse'])


def fstab(fqdn, device=None):
    shortname = get_shortname(fqdn)
    sudo('mkdir -p %s/mnt/btrfs' % env.dest)
    sudo('genfstab -L "%s" > "%s/etc/fstab"' % (env.dest, env.dest))
    if device:
        sudo('echo "LABEL=%s-btrfs /mnt/btrfs btrfs defaults,volid=0 0 0"'
             '>> %s/etc/fstab' % (shortname, env.dest))


def network_config(fqdn):
    shortname = get_shortname(fqdn)
    sudo('echo "%s" > "%s/etc/hostname"' % (shortname, env.dest))
    sudo('echo "127.0.1.1\t%s\t%s" >> %s/etc/hosts'
         % (fqdn, shortname, env.dest))
    enable_services(['NetworkManager'])


def boot_loader(root_label=None, efi=True):
    if root_label:
        if efi:
            boot_loader_entry = """title    Arch Linux
linux    /vmlinuz-linux
initrd   /initramfs-linux.img
options  root=LABEL=%s rw
EOF""" % root_label
            pacstrap(['gummiboot'])
            sudo('arch-chroot %s gummiboot install' % env.dest)
            sudo("cat <<-EOF > %s/boot/loader/entries/arch.conf\n" % env.dest +
                 boot_loader_entry)
        else:
            pacstrap(['syslinux'])
            sudo('sed -i "s|APPEND root=/dev/sda3|APPEND root=LABEL=%s|g"'
                 ' "%s/boot/syslinux/syslinux.cfg"' % (root_label, env.dest))
            sudo('sed -i "/TIMEOUT/s/^.*$/TIMEOUT 10/"'
                 ' "%s/boot/syslinux/syslinux.cfg"' % env.dest)
            sudo('arch-chroot "%s" /usr/bin/syslinux-install_update -iam'
                 % env.dest)
    sudo('arch-chroot "%s" /usr/bin/mkinitcpio -p linux' % env.dest)


def booleanize(value):
    """Return value as a boolean."""

    true_values = ("yes", "y", "Y", "true", "True", "1")
    false_values = ("no", "n", "N", "false", "False", "0")

    if isinstance(value, bool):
        return value

    if value.lower() in true_values:
        return True
    elif value.lower() in false_values:
        return False
    else:
        raise TypeError("Cannot booleanize ambiguous value '%s'" % value)


def chroot_puppet():
    script = """#!/bin/bash
export LANG=en_US.UTF-8
export LC_CTYPE=en_US.UTF-8
export LC_ALL=en_US.UTF-8
hostname $(cat %s/etc/hostname)
rm -rf /etc/puppet /etc/hieradata
git clone https://github.com/justin8/puppet /etc/puppet
git -C /etc/puppet submodule update --init
git clone https://github.com/justin8/hieradata /etc/hieradata
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default::misc'
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default::package_manager'
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default'
EOF""" % env.dest
    sudo("cat <<-EOF > %s/var/tmp/puppet.sh\n" % env.dest + script, quiet=True)
    sudo('chmod +x %s/var/tmp/puppet.sh' % env.dest, quiet=True)
    # Set warn only as puppet uses return codes when it is successful
    puppet = sudo('arch-chroot "%s" /var/tmp/puppet.sh' % env.dest,
                  warn_only=True, quiet=env.quiet)
    if puppet.return_code not in [0, 2]:
        print("*****Puppet returned a critical error*****")
        print(puppet)
        raise RuntimeError('Puppet encountered an error during execution.'
                           ' rc=%s' % puppet.return_code)


def enable_services(services):
    for service in services:
        sudo("arch-chroot %s systemctl enable %s"
             % (env.dest, service), quiet=env.quiet)


def set_locale():
    sudo('echo LANG=en_AU.utf8 > /etc/locale.conf')
    sudo('echo "en_AU.UTF-8 UTF-8" > /etc/locale.gen')
    sudo('arch-chroot %s locale-gen' % env.dest)


def gui_install():
    print('*** Installing GUI packages...')
    pacstrap(gui_packages)

    print('*** Configuring GUI services...')
    enable_services(gui_services)


def get_shortname(fqdn):
    # Fix this to work if there is no fqdn and only has a short name
    if re.search('\.', fqdn):
        return re.search('^(.*?)\..+', fqdn).groups()[0]
    else:
        return fqdn


def cleanup(device):
    print('*** Cleaning up...')
    while sudo('umount -l %s1' % device, warn_only=True).return_code == 0:
        pass
    while sudo('umount -l %s2' % device, warn_only=True).return_code == 0:
        pass
    sudo('rmdir %s' % env.dest)


def install_ssh_key(keyfile):
    sudo('mkdir %s/root/.ssh' % env.dest, quiet=True)
    sudo('chmod 700 %s/root/.ssh' % env.host, quiet=True)
    put(local_path=keyfile,
        remote_path='%s/root/.ssh/authorized_keys' % env.dest,
        use_sudo=True,
        mode=0600)


def dotfiles_install(remote):
    if remote:
        script = """#!/bin/bash
            git clone https://github.com/justin8/dotfiles /var/tmp/dotfiles
            /var/tmp/dotfiles/install"""
    else:
        script = """#!/bin/bash
            mount /var/cache/pacman/pkg
            git clone https://github.com/justin8/dotfiles /var/tmp/dotfiles
            /var/tmp/dotfiles/install
            umount -l /var/cache/pacman/pkg"""

    sudo('echo "%s" > %s/var/tmp/dotfiles-install' % (script, env.dest))
    sudo('chmod +x %s/var/tmp/dotfiles-install' % env.dest)
    sudo('arch-chroot "%s" /var/tmp/dotfiles-install' % env.dest)


def prepare_device_efi(device, shortname, boot, root):
    sudo('echo -e "o\ny\nn\n\n\n+200M\nef00\nn\n\n\n\n\nw\ny\n" | gdisk "%s"'
         % device, quiet=True)
    sudo('wipefs -a %s' % boot)
    sudo('wipefs -a %s' % root)
    sudo('mkfs.vfat -F32 %s -n "boot"' % boot)


def prepare_device_bios(device, shortname, boot, root):
    # Use parted to create a blank partition table, it correctly clears GPT
    # tables as well, unlike fdisk
    sudo('parted -s %s mklabel msdos' % device)
    sudo('echo -e "n\n\n\n\n+200M\nn\n\n\n\n\nw\n" | fdisk "%s"'
         % device, quiet=True)
    sudo('wipefs -a %s' % boot)
    sudo('wipefs -a %s' % root)
    sudo('mkfs.ext4 -m 0 -L "boot" "%s"' % boot)


@task
def install_os(fqdn, efi=True, gpu=False, device=None, mountpoint=None,
               gui=False, ssh_key=None, quiet=False, extra_packages=None,
               remote=None, new_password=None):
    """
    If specified, gpu must be one of: nvidia, nouveau, amd, intel or vbox.
    If new_password is specified it will be set as the root password on the
    machine. Otherwise a random password will be set for security purposes.

    gpu: Should be one of: nvidia, nouveau, ati, intel, vbox
    gui: Will configure a basic gnome environment
    remote: Set if not building locally to abachi. Should be auto detected if not set.
    """

    efi = booleanize(efi)
    gui = booleanize(gui)
    quiet = booleanize(quiet)

    if gui and not gpu:
        raise RuntimeError("You must specify a GPU if GUI is selected")

    env.quiet = quiet

    # Sanity checks
    if not fqdn:
        raise RuntimeError("You must specify an fqdn!")
    shortname = get_shortname(fqdn)

    if not device and not mountpoint or device and mountpoint:
        raise RuntimeError(
            "You must specify either a device or a mountpoint but not both")

    if gpu and gpu not in valid_gpus:
        raise RuntimeError("Invalid gpu specified")

    if ssh_key:
        if not os.path.isfile(ssh_key):
            raise RuntimeError("The specified SSH key cannot be found!")

    if remote is None:
        # Auto detect if we are remote or not. Copied from facter fact
        remote = False
        if sudo("nslookup abachi.dray.be | grep -o '192.168.1.15'") == '192.168.1.15':
            if sudo("ip route|grep default|grep -o 192.168.1.1") == '192.168.1.1':
                remote = False

    if device:
        # check device exists
        if sudo('test -b %s' % device, quiet=True).return_code != 0:
            raise RuntimeError("The device specified is not a device!")

        env.dest = sudo('mktemp -d')

        # TODO: unmount all partitions on the device if they are mounted

        # Create partitions; 200M sdX1 and the rest as sdX2
        print("*** Preparing device...")
        boot = '%s1' % device
        root = '%s2' % device
        if efi:
            prepare_device_efi(device, shortname, boot=boot, root=root)
        else:
            prepare_device_bios(device, shortname, boot=boot, root=root)

        sudo('mkfs.btrfs -L "%s-btrfs" "%s"' % (shortname, root))

        # Set up root as the default btrfs subvolume
        try:
            sudo('mount "%s" "%s"' % (root, env.dest))
            sudo('btrfs subvolume create "%s/root"' % env.dest)
            subvols = sudo('btrfs subvolume list "%s"' % env.dest, quiet=True)
            subvolid = re.findall('ID (\d+).*level 5 path root$',
                                  subvols, re.MULTILINE)[0]
            sudo('btrfs subvolume set-default "%s" "%s"'
                 % (subvolid, env.dest))
            sudo('umount -l "%s"' % env.dest)

            # Mount all of the things
            sudo('mount -o relatime "%s" "%s"' % (root, env.dest))
            sudo('mkdir "%s/boot"' % env.dest)
            sudo('mount "%s" "%s/boot"' % (boot, env.dest))
        except:
            cleanup(device)
    elif mountpoint:
        env.dest = mountpoint
        mounts = sudo('mount', quiet=True)
        if not re.search('\s%s\s+type' % env.dest, mounts):
            raise RuntimeError("The specified mountpoint is not mounted")
    try:
        print('*** Enabling dray.be repo...')
        enable_dray_repo()

        print('*** Enabling multilib repo...')
        enable_multilib_repo()

        print('*** Enabling mDNS...')
        enable_mdns()

        if not remote:
            print('*** Mounting package cache...')
            out = sudo('mount -t nfs abachi.local:/pacman /var/cache/pacman/pkg', warn_only=True)
            if out.return_code not in {32, 0}:
                print("Failed to mount package cache. Aborting")
                sys.exit(1)

        print("*** Installing base OS...")
        pacstrap(base_packages)

        if not new_password:
            new_password = generate_password(16)
        print('*** Setting root password...')
        sudo('echo "root:%s" | arch-chroot "%s" chpasswd'
             % (new_password, env.dest), quiet=True)

        if ssh_key:
            print("*** Installing ssh key...")
            install_ssh_key(ssh_key)

        print("*** Configuring network...")
        network_config(fqdn)

        print("*** Configuring base system services...")
        enable_services(base_services)

        print('*** Generating fstab...')
        fstab(fqdn, device)

        print('*** Setting initial locale...')
        set_locale()

        print("*** Configuring base system via puppet...")
        chroot_puppet()

        if gpu:
            print('*** Installing graphics drivers...')
            gpu_install(gpu)

        if gui:
            print('*** Installing GUI packages...')
            gui_install()

        print("*** Installing root dotfiles configuration...")
        dotfiles_install(remote)

        if extra_packages:
            print("*** Installing additional packages...")
            pacstrap(extra_packages)

        print('*** Installing boot loader...')
        if device:
            boot_loader('%s-btrfs' % shortname, efi=efi)
        else:
            boot_loader(efi=efi)
            print("Make sure to configure the boot loader since no device was specified!")
    finally:
        if device:
            cleanup(device)
