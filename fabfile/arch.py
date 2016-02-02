
from __future__ import print_function

import os
import random
import re
import string
import sys

from fabric.api import env, put, sudo, task

env.quiet = False
valid_gpus = ['auto', 'nvidia', 'nouveau', 'amd', 'intel', 'vbox', 'vmware']
base_packages = [
    'base', 'btrfs-progs', 'cronie', 'dkms', 'git', 'gptfdisk', 'networkmanager',
    'nfs-utils', 'pkgfile', 'puppet3', 'openssh', 'rsync', 'tzupdate', 'vim', 'zsh']
base_services = ['cronie', 'dkms', 'puppet', 'sshd']
gui_packages = [
    'aspell-en', 'gdm', 'gnome', 'gnome-tweak-tool', 'terminator', 'ttf-dejavu']
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
    pacstrap -c "{0}" {1} | tee /tmp/out
    if grep 'invalid or corrupted package' /tmp/out
    then
        count=$((count+1))
        echo "Failed $count times!"
    else
        break
    fi
done
EOF""".format(env.dest, ' '.join(packages))
    sudo("cat <<-'EOF' > /tmp/pacstrap.sh\n" + script, quiet=True)
    sudo('chmod +x /tmp/pacstrap.sh', quiet=True)
    sudo('/tmp/pacstrap.sh', quiet=env.quiet)


def chroot(command, warn_only=False, quiet=False):
    sudo('arch-chroot {0} "{1}"'.format(env.dest, command), warn_only=warn_only, quiet=quiet)


def enable_multilib_repo():
    if not sudo("grep -q '^\[multilib\]' /etc/pacman.conf", warn_only=True).succeeded:
        sudo('echo [multilib] >> /etc/pacman.conf')
        sudo('echo Include = /etc/pacman.d/mirrorlist >> /etc/pacman.conf')


def enable_dray_repo():
    sudo('curl -o /tmp/repo.pkg.tar.xz https://repo.dray.be/dray-repo-latest')
    sudo('pacman -U --noconfirm /tmp/repo.pkg.tar.xz')


def enable_mdns(target):
    cmd = sudo if target is 'host' else chroot
    cmd('pacman -Sy --noconfirm avahi nss-mdns')
    cmd("sed -i 's/^hosts.*/hosts: files mdns_minimal [NOTFOUND=return] dns myhostname/' /etc/nsswitch.conf")
    cmd('nscd -i hosts', warn_only=True, quiet=True)


def gpu_detect():
    lspci = sudo('lspci|grep VGA').lower()
    if 'intel' in lspci:
        return 'intel'
    if 'nvidia' in lspci:
        return 'nvidia'
    if 'amd' in lspci:
        return 'amd'
    if 'virtualbox' in lspci:
        return 'vbox'


def gpu_install(gpu):
    if gpu == 'nvidia':
        gpu_packages = ['lib32-mesa', 'lib32-nvidia-libgl', 'nvidia-libgl', 'nvidia-dkms']
    if gpu == 'nouveau':
        gpu_packages = ['lib32-mesa', 'xf86-video-nouveau']
        sudo("""sed -i '/MODULES=/s/"$/ nouveau"/' %s/etc/mkinitcpio.conf"""
             % env.dest)
    if gpu == 'amd':
        gpu_packages = ['lib32-mesa', 'xf86-video-ati', 'mesa-libgl', 'lib32-mesa-libgl', 'mesa-vdpau', 'lib32-mesa-vdpau']
    if gpu == 'intel':
        gpu_packages = ['lib32-mesa', 'xf86-video-intel']
    if gpu == 'vbox':
        gpu_packages = ['virtualbox-guest-dkms', 'virtualbox-guest-utils']
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


def generate_fstab(fqdn, device=None):
    sudo('genfstab -L "{0}" > "{0}/etc/fstab"'.format(env.dest))


def network_config(fqdn):
    shortname = get_shortname(fqdn)
    sudo('echo "{0}" > "{1}/etc/hostname"'.format(shortname, env.dest))
    sudo('echo "127.0.1.1\t{0}\t{1}" >> {2}/etc/hosts'.format(fqdn, shortname, env.dest))
    enable_services(['NetworkManager'])


def boot_loader(efi, kernel):
    root_label = get_root_label()
    intel = not bool(sudo('grep GenuineIntel /proc/cpuinfo', warn_only=True).return_code)
    kernel_string = 'linux'

    if intel:
        pacstrap(['intel-ucode'])
    if kernel:
        pacstrap(['linux-%s' % kernel])
        kernel_string = 'linux-%s' % kernel
    if kernel == 'grsec':
        pacstrap(['paxd'])
    if efi:
        ucode_string = "\ninitrd   /intel-ucode.img" if intel else ''
        boot_loader_entry = """title    Arch Linux
linux    /vmlinuz-""" + kernel_string + ucode_string + """
initrd   /initramfs-{0}.img
options  root=LABEL={1} rw
EOF""".format(kernel_string, root_label)
        chroot('bootctl install')
        sudo("cat <<-EOF > %s/boot/loader/entries/arch.conf\n" % env.dest +
             boot_loader_entry)
    else:
        pacstrap(['syslinux'])
        sudo('sed -i "s|APPEND root=/dev/sda3|APPEND root=LABEL={0}|g"'
             ' "{1}/boot/syslinux/syslinux.cfg"'.format(root_label, env.dest))
        sudo('sed -i "/TIMEOUT/s/^.*$/TIMEOUT 10/"'
             ' "%s/boot/syslinux/syslinux.cfg"' % env.dest)
        sudo('sed -i "s/vmlinuz-linux/vmlinuz-{0}/"'
             ' "{1}/boot/syslinux/syslinux.cfg"'.format(kernel_string, env.dest))
        sudo('sed -i "s/initramfs-linux/initramfs-{0}/"'
             ' "{1}/boot/syslinux/syslinux.cfg"'.format(kernel_string, env.dest))
        if intel:
            sudo('sed -i "/initramfs-' + kernel_string + '.img/s|INITRD|INITRD ../intel-ucode'
                 r'.img\n    INITRD|" "' + env.dest + '/boot/syslinux/syslinux.cfg"')
        chroot('/usr/bin/syslinux-install_update -iam')
    chroot('/usr/bin/mkinitcpio -p %s' % kernel_string)


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


@task
def chroot_puppet(dest):
    script = """#!/bin/bash -x
export LANG=en_US.UTF-8
export LC_CTYPE=en_US.UTF-8
export LC_ALL=en_US.UTF-8
hostname $(cat %s/etc/hostname)
rm -rf /etc/puppet /etc/hieradata
git clone https://github.com/justin8/puppet /etc/puppet
echo -n "Updating submodules... "
git -C /etc/puppet submodule update --init &> /dev/null
[[ $? == 0 ]] && echo "[ OK ]" || echo "[ FAIL ]"
git clone https://github.com/justin8/hieradata /etc/hieradata
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default::os_specifics'
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default::misc'
puppet apply --modulepath=/etc/puppet/modules --test -e 'include os_default'
EOF""" % dest
    sudo("cat <<-EOF > %s/var/tmp/puppet.sh\n" % dest + script, quiet=True)
    sudo('chmod +x %s/var/tmp/puppet.sh' % dest, quiet=True)
    # Set warn only as puppet uses return codes when it is successful
    puppet = chroot('/var/tmp/puppet.sh', warn_only=True, quiet=env.quiet)
    if puppet.return_code not in [0, 2]:
        print("*****Puppet returned a critical error*****")
        print(puppet)
        raise RuntimeError('Puppet encountered an error during execution.'
                           ' rc=%s' % puppet.return_code)


def enable_services(services):
    for service in services:
        chroot("systemctl enable " + service, quiet=env.quiet)


def set_locale():
    sudo('echo LANG=en_AU.utf8 > /etc/locale.conf')
    sudo('echo "en_AU.UTF-8 UTF-8" > /etc/locale.gen')
    chroot('locale-gen')


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
            mount /var/cache/pacman/pkg || true
            git clone https://github.com/justin8/dotfiles /var/tmp/dotfiles
            /var/tmp/dotfiles/install
            umount -l /var/cache/pacman/pkg || true"""

    sudo('echo "%s" > %s/var/tmp/dotfiles-install' % (script, env.dest))
    sudo('chmod +x %s/var/tmp/dotfiles-install' % env.dest)
    chroot('/var/tmp/dotfiles-install')


def get_root_label():
    device = sudo("mount | grep ' on %s ' | awk '{print $1}'" % env.dest, quiet=True)
    return sudo("lsblk -o label %s | tail -n1" % device, quiet=True)


def get_boot_and_root(device):
    return ['%s1' % device, '%s2' % device]


def create_efi_layout(device, shortname):
    boot, root = get_boot_and_root(device)
    sudo('echo -e "o\ny\nn\n\n\n+200M\nef00\nn\n\n\n\n\nw\ny\n" | gdisk "%s"'
         % device, quiet=True)
    sudo('wipefs -a %s' % boot)
    sudo('wipefs -a %s' % root)
    sudo('mkfs.vfat -F32 %s -n "boot"' % boot)


def create_bios_layout(device, shortname):
    # Use parted to create a blank partition table, it correctly clears GPT
    # tables as well, unlike fdisk
    boot, root = get_boot_and_root(device)
    sudo('parted -s %s mklabel msdos' % device)
    sudo('echo -e "n\n\n\n\n+200M\nn\n\n\n\n\nw\n" | fdisk "%s"'
         % device, quiet=True)
    sudo('wipefs -a %s' % boot)
    sudo('wipefs -a %s' % root)
    sudo('mkfs.ext4 -m 0 -L "boot" "%s"' % boot)


def prepare_device(device, shortname, efi):
    # TODO: unmount all partitions on the device if they are mounted
    # Create partitions; 200M sdX1 and the rest as sdX2. Layout differs for EFI
    if efi:
        create_efi_layout(device, shortname)
    else:
        create_bios_layout(device, shortname)

    boot, root = get_boot_and_root(device)

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


def set_timezone():
    chroot('tzupdate')


@task
def install_os(fqdn, efi=True, gpu='auto', device=None, mountpoint=None,
               gui=False, ssh_key=None, quiet=env.quiet, kernel='lts', extra_packages=None,
               remote=None, new_password=None):
    """
    If specified, gpu must be one of: nvidia, nouveau, amd, intel or vbox.
    If new_password is specified it will be set as the root password on the
    machine. Otherwise a random password will be set for security purposes.

    gpu: Should be one of: auto, nvidia, nouveau, ati, intel, vbox. Defaults to auto.
    gui: Will configure a basic gnome environment
    kernel: Can be 'lts', 'grsec', or other kernels in the repositories or '' for default kernel.
    remote: Set if not building locally to abachi. Should be auto detected if not set.
    """

    efi = booleanize(efi)
    gui = booleanize(gui)
    quiet = booleanize(quiet)

    env.quiet = quiet

    # Sanity checks
    if not fqdn:
        raise RuntimeError("You must specify an fqdn!")
    shortname = get_shortname(fqdn)

    if not device and not mountpoint or device and mountpoint:
        raise RuntimeError(
            "You must specify either a device or a mountpoint but not both")

    if gpu not in valid_gpus:
        raise RuntimeError("Invalid gpu specified")

    if ssh_key:
        if not os.path.isfile(ssh_key):
            raise RuntimeError("The specified SSH key cannot be found!")

    if remote is None:
        # Auto detect if we are remote or not. Copied from facter fact
        remote = True
        if sudo("nslookup abachi.dray.be | grep -o '192.168.1.15'", warn_only=True) == '192.168.1.15':
            if sudo("ip route|grep default|grep -o 192.168.1.1") == '192.168.1.1':
                remote = False

    if device:
        if sudo('test -b %s' % device, quiet=True).return_code != 0:
            raise RuntimeError("The device specified is not a device!")

        env.dest = sudo('mktemp -d')

        print('*** Preparing device...')
        prepare_device(device, shortname, efi)
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

        print('*** Enabling mDNS during install...')
        enable_mdns('host')

        if not remote:
            print('*** Mounting package cache...')
            out = sudo('mount -t nfs abachi.local:/pacman /var/cache/pacman/pkg', warn_only=True)
            if out.return_code not in {32, 0}:
                print("Failed to mount package cache. Aborting")
                sys.exit(1)

        print('*** Installing base OS...')
        pacstrap(base_packages)

        if not new_password:
            new_password = generate_password(16)
        print('*** Setting root password...')
        sudo('echo "root:%s" | arch-chroot "%s" chpasswd'
             % (new_password, env.dest), quiet=True)

        if ssh_key:
            print('*** Installing ssh key...')
            install_ssh_key(ssh_key)

        print('*** Configuring network...')
        network_config(fqdn)

        print('*** Configuring mDNS...')
        enable_mdns('chroot')

        print('*** Configuring base system services...')
        enable_services(base_services)

        print('*** Generating fstab...')
        generate_fstab(fqdn, device)

        print('*** Setting up cron jobs...')
        create_cron_job('create-package-list', 'pacman -Qe > /etc/package-list', time='daily')
        create_cron_job('udpate-pkgfile', 'pkgfile -u &>/dev/null', time='daily')

        print('*** Setting default locale...')
        set_locale()

        print('*** Setting default timezone...')
        set_timezone()

        print("*** Configuring base system via puppet...")
        chroot_puppet(env.dest)

        if gpu == 'auto':
            print('*** Detecting graphics card...')
            gpu = gpu_detect()
            print('*** Found {0}...'.format(gpu))

        print('*** Installing graphics drivers...')
        gpu_install(gpu)

        if gui:
            print('*** Installing GUI packages...')
            gui_install()

        print('*** Installing root dotfiles configuration...')
        dotfiles_install(remote)

        if extra_packages:
            print('*** Installing additional packages...')
            pacstrap(extra_packages)

        print('*** Installing boot loader...')
        boot_loader(efi=efi, kernel=kernel)

    finally:
        if device:
            cleanup(device)
