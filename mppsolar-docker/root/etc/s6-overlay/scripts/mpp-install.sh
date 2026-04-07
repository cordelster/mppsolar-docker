#!/bin/bash
# Dev mode: install mpp-solar from a mounted source tree, overriding the APK version.
# Production: /src/mpp-solar is not mounted — py3-jb-mppsolar from JamBox APK is used.

if [ ! -d /src/mpp-solar ]; then
    echo "[mpp-install] /src/mpp-solar not mounted — using pre-installed py3-jb-mppsolar"
    exit 0
fi

echo "[mpp-install] Dev mode: pip install -e /src/mpp-solar"
pip3 install --quiet --break-system-packages -e /src/mpp-solar
echo "[mpp-install] done"
