#!/usr/bin/env sh

set -eux

patched_glibc=glibc-linux4-2.33-4-x86_64.pkg.tar.zst && curl -LO "https://repo.archlinuxcn.org/x86_64/$patched_glibc" && bsdtar -C / -xvf "$patched_glibc"
cd /varnish-cache
tar xazf arch.tar.gz --strip 1

useradd builder
echo "builder ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers

echo "Fix PKGBUILD's variables"
tar xavf varnish-*.tar.gz
VERSION=$(varnish-*/configure --version | awk 'NR == 1 {print $NF}')
echo "Version: $VERSION"
sed -i "s/@VERSION@/$VERSION/" PKGBUILD
rm -rf varnish-*/

chown builder -R .
echo "Build"
su builder -c "makepkg -rsf --noconfirm --skipinteg"

echo "Fix the APKBUILD's version"
find /home
su builder -c "mkdir apks"
ARCH=`uname -m`
su builder -c "cp /home/builder/packages/$ARCH/*.apk apks"

echo "Import the packages into the workspace"
mkdir -p packages/$PARAM_DIST/$PARAM_RELEASE/$ARCH/
mv /home/builder/packages/$ARCH/*.apk packages/$PARAM_DIST/$PARAM_RELEASE/$ARCH/

echo "Allow to read the packages by 'circleci' user outside of Docker after 'chown builder -R .' above"
chmod -R a+rwx .
