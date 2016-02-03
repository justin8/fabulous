#!/bin/bash

setup_environment() {
	echo root:root | chpasswd

	if ! grep -q '^PermitRootLogin yes' /etc/ssh/sshd_config; then
		echo 'PermitRootLogin yes' >> /etc/ssh/sshd_config
	fi

	if ! nc -z localhost 22; then
		systemctl restart sshd
	fi

	if ! fab -h &>/dev/null; then
		pacman -Sy fabric
	fi
}

if ! [[ -d fabfile ]]; then
	cd ~/fabulous
fi

if [[ $EUID != 0 ]]; then
	echo 'You must run this script as root!'
	exit 1
fi

echo "Please enter the following..."
read -rp "Host name: " hostname
read -rp "Username: " username
read -rsp "Password: " password
echo
read -rp "GUI (y/N): " gui
read -rp 'Device or mountpoint to install to (default: /dev/sda):' target
target=${target:-/dev/sda}

[[ $gui =~ [yY] ]] && gui=true || gui=false

setup_environment

fab -H localhost -u root -p root arch.install_os:fqdn="$hostname",target="$target",username="$username",password="$password",gui=$gui
