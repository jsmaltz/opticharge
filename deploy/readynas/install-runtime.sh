#!/bin/sh
set -eu

BASE_DIR=${BASE_DIR:-/apps/opticharge}
APP_DIR=${APP_DIR:-$BASE_DIR/app}
OPENSSL_VERSION=${OPENSSL_VERSION:-1.1.1w}
PYTHON_VERSION=${PYTHON_VERSION:-3.10.14}
OPENSSL_PREFIX="$BASE_DIR/openssl-$OPENSSL_VERSION"
PYTHON_PREFIX="$BASE_DIR/python-$PYTHON_VERSION"
VENV_DIR="$BASE_DIR/venv"

if [ "$(id -u)" != "0" ]; then
    echo "Run as root on the ReadyNAS." >&2
    exit 1
fi

if [ ! -f "$APP_DIR/requirements.txt" ]; then
    echo "Expected app checkout at $APP_DIR with requirements.txt" >&2
    exit 1
fi

mkdir -p "$BASE_DIR/src" "$BASE_DIR/bootstrap-debs" "$BASE_DIR/toolchain-debs" \
    "$BASE_DIR/dev-debs" "$BASE_DIR/repacked/out" "$BASE_DIR/logs"

configure_archive_apt() {
    if ! grep -q "archive.debian.org/debian jessie" /etc/apt/sources.list; then
        cp -a /etc/apt/sources.list \
            "/etc/apt/sources.list.opticharge-backup.$(date +%Y%m%d%H%M%S)"
        cat > /etc/apt/sources.list <<'EOF'
deb https://apt.readynas.com/packages/readynasos 6.10.10 updates apps main

deb http://archive.debian.org/debian jessie main
deb http://archive.debian.org/debian-security jessie/updates main
EOF
    fi
    printf '%s\n' 'Acquire::Check-Valid-Until "false";' \
        > /etc/apt/apt.conf.d/99opticharge-archive
    apt-get update
}

download_debs() {
    dest=$1
    shift
    mkdir -p "$dest"
    cd "$dest"
    apt-get --allow-unauthenticated download "$@"
}

install_libc_headers() {
    cd "$BASE_DIR/bootstrap-debs"
    download_debs "$BASE_DIR/bootstrap-debs" \
        libc-dev-bin=2.19-18+deb8u10 \
        linux-libc-dev=3.16.84-1 \
        libc6-dev=2.19-18+deb8u10

    installed_libc=$(dpkg-query -W -f='${Version}' libc6:armel 2>/dev/null || true)
    if [ "$installed_libc" = "2.19-18+deb8u10.netgear1" ]; then
        rm -rf "$BASE_DIR/repacked/libc-dev-bin" "$BASE_DIR/repacked/libc6-dev"
        dpkg-deb -R "$BASE_DIR/bootstrap-debs/libc-dev-bin_2.19-18+deb8u10_armel.deb" \
            "$BASE_DIR/repacked/libc-dev-bin"
        dpkg-deb -R "$BASE_DIR/bootstrap-debs/libc6-dev_2.19-18+deb8u10_armel.deb" \
            "$BASE_DIR/repacked/libc6-dev"
        sed -i 's/^Version: 2.19-18+deb8u10$/Version: 2.19-18+deb8u10.netgear1/' \
            "$BASE_DIR/repacked/libc-dev-bin/DEBIAN/control"
        sed -i \
            -e 's/^Version: 2.19-18+deb8u10$/Version: 2.19-18+deb8u10.netgear1/' \
            -e 's/libc6 (= 2.19-18+deb8u10)/libc6 (= 2.19-18+deb8u10.netgear1)/' \
            -e 's/libc-dev-bin (= 2.19-18+deb8u10)/libc-dev-bin (= 2.19-18+deb8u10.netgear1)/' \
            "$BASE_DIR/repacked/libc6-dev/DEBIAN/control"
        dpkg-deb -b "$BASE_DIR/repacked/libc-dev-bin" \
            "$BASE_DIR/repacked/out/libc-dev-bin_2.19-18+deb8u10.netgear1_armel.deb"
        dpkg-deb -b "$BASE_DIR/repacked/libc6-dev" \
            "$BASE_DIR/repacked/out/libc6-dev_2.19-18+deb8u10.netgear1_armel.deb"
        dpkg -i "$BASE_DIR/repacked/out/libc-dev-bin_2.19-18+deb8u10.netgear1_armel.deb" \
            "$BASE_DIR/bootstrap-debs/linux-libc-dev_3.16.84-1_armel.deb" \
            "$BASE_DIR/repacked/out/libc6-dev_2.19-18+deb8u10.netgear1_armel.deb"
    else
        dpkg -i --force-depends \
            "$BASE_DIR/bootstrap-debs/libc-dev-bin_2.19-18+deb8u10_armel.deb" \
            "$BASE_DIR/bootstrap-debs/linux-libc-dev_3.16.84-1_armel.deb" \
            "$BASE_DIR/bootstrap-debs/libc6-dev_2.19-18+deb8u10_armel.deb"
    fi
}

install_toolchain_and_headers() {
    download_debs "$BASE_DIR/toolchain-debs" \
        perl libasan1 libatomic1 libisl10 libcloog-isl4 libmpfr4 libubsan0 \
        libmpc3 bzip2 patch binutils cpp-4.9 cpp libgcc-4.9-dev gcc-4.9 \
        gcc libstdc++-4.9-dev g++-4.9 g++ make pkg-config xz-utils wget \
        ca-certificates
    dpkg -i --force-depends "$BASE_DIR"/toolchain-debs/*.deb

    download_debs "$BASE_DIR/dev-debs" \
        zlib1g=1:1.2.8.dfsg-2+deb8u1 zlib1g-dev=1:1.2.8.dfsg-2+deb8u1 \
        libffi-dev libbz2-dev liblzma-dev libreadline6-dev libreadline-dev \
        libncursesw5-dev libtinfo-dev libgdbm-dev
    dpkg -i --force-depends "$BASE_DIR"/dev-debs/*.deb
}

build_openssl() {
    if [ -x "$OPENSSL_PREFIX/bin/openssl" ]; then
        LD_LIBRARY_PATH="$OPENSSL_PREFIX/lib" "$OPENSSL_PREFIX/bin/openssl" version
        return
    fi

    cd "$BASE_DIR/src"
    if [ ! -f "openssl-$OPENSSL_VERSION.tar.gz" ]; then
        wget -O "openssl-$OPENSSL_VERSION.tar.gz" \
            "https://www.openssl.org/source/openssl-$OPENSSL_VERSION.tar.gz"
    fi
    rm -rf "openssl-$OPENSSL_VERSION"
    tar -xzf "openssl-$OPENSSL_VERSION.tar.gz"
    cd "openssl-$OPENSSL_VERSION"
    ./config --prefix="$OPENSSL_PREFIX" --openssldir="$OPENSSL_PREFIX/ssl" \
        shared no-zlib
    make -j2
    make install_sw
    LD_LIBRARY_PATH="$OPENSSL_PREFIX/lib" "$OPENSSL_PREFIX/bin/openssl" version
}

build_python() {
    if [ -x "$PYTHON_PREFIX/bin/python3.10" ]; then
        "$PYTHON_PREFIX/bin/python3.10" -V
        return
    fi

    cd "$BASE_DIR/src"
    if [ ! -f "Python-$PYTHON_VERSION.tgz" ]; then
        wget -O "Python-$PYTHON_VERSION.tgz" \
            "https://www.python.org/ftp/python/$PYTHON_VERSION/Python-$PYTHON_VERSION.tgz"
    fi
    rm -rf "Python-$PYTHON_VERSION"
    tar -xzf "Python-$PYTHON_VERSION.tgz"
    cd "Python-$PYTHON_VERSION"
    export PKG_CONFIG_PATH="$OPENSSL_PREFIX/lib/pkgconfig"
    export LDFLAGS="-Wl,-rpath,$OPENSSL_PREFIX/lib -L$OPENSSL_PREFIX/lib"
    export CPPFLAGS="-I$OPENSSL_PREFIX/include"
    ./configure --prefix="$PYTHON_PREFIX" \
        --with-openssl="$OPENSSL_PREFIX" \
        --with-openssl-rpath=auto \
        --with-ensurepip=install \
        --disable-test-modules
    make -j2
    make install
    "$PYTHON_PREFIX/bin/python3.10" -c \
        'import ssl,zlib,bz2,lzma,ctypes; print(ssl.OPENSSL_VERSION); print(zlib.ZLIB_VERSION)'
}

install_venv() {
    if [ ! -x "$VENV_DIR/bin/python" ]; then
        "$PYTHON_PREFIX/bin/python3.10" -m venv "$VENV_DIR"
    fi
    "$VENV_DIR/bin/python" -m pip install --upgrade pip setuptools wheel
    cd "$APP_DIR"
    "$VENV_DIR/bin/python" -m pip install -r requirements.txt
    "$VENV_DIR/bin/python" -m pip check
    "$VENV_DIR/bin/python" -m py_compile opticharge.py readteslaonly.py tesla_fleet.py tesla_local.py tesla_local_password.py tesla_auth.py tesla_diagnose.py
}

install_service() {
    service_template="$APP_DIR/deploy/readynas/opticharge.service.in"
    if [ ! -f "$service_template" ]; then
        echo "Missing service template: $service_template" >&2
        exit 1
    fi
    sed -e "s|@BASE_DIR@|$BASE_DIR|g" -e "s|@APP_DIR@|$APP_DIR|g" \
        "$service_template" > /etc/systemd/system/opticharge.service
    systemctl daemon-reload
    systemctl enable opticharge.service
}

configure_archive_apt
install_libc_headers
install_toolchain_and_headers
build_openssl
build_python
install_venv
install_service
apt-get check

echo "ReadyNAS runtime installed."
echo "Start with: systemctl start opticharge.service"
