# ReadyNASOS 6.10.10 deployment

This directory captures the ReadyNAS port as a reproducible deployment layer.
Do not commit NAS-local build outputs, virtual environments, downloaded tarballs,
or `config.yaml`.

## Target tested

- ReadyNASOS 6.10.10
- Debian 8 Jessie userland
- `armel` on ARMv7
- System OpenSSL 1.0.1t, which is too old for Python 3.10

The bootstrap builds a private runtime under `/apps/opticharge`:

- OpenSSL 1.1.1w: `/apps/opticharge/openssl-1.1.1w`
- Python 3.10.14: `/apps/opticharge/python-3.10.14`
- App venv: `/apps/opticharge/venv`
- App checkout: `/apps/opticharge/app`

## First install

Copy or clone this repository to the NAS at `/apps/opticharge/app`, then:

```sh
cd /apps/opticharge/app
cp config.yaml_template config.yaml
vi config.yaml
sh deploy/readynas/install-runtime.sh
systemctl start opticharge.service
```

The script enables the service for boot, but does not start it until you run
`systemctl start opticharge.service`.

## Service commands

```sh
systemctl status opticharge.service
journalctl -u opticharge.service -f
systemctl restart opticharge.service
systemctl stop opticharge.service
```

## Why this is not just apt install

ReadyNASOS 6.10.10 uses Debian Jessie. Jessie only offers Python 3.4 via apt,
while OptiCharge and `hyundai_kia_connect_api` require Python 3.10+. The system
OpenSSL is also too old. The script therefore builds private OpenSSL and Python
installations and keeps them out of the system path.

The NETGEAR ReadyNAS apt repo may advertise development packages whose `.deb`
URLs now return HTTP 403. To keep apt healthy, the script uses Debian archive
build headers and repacks the libc development package metadata to match the
installed NETGEAR libc version. Runtime libc is not replaced.

