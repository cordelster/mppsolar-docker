ARG BUILD_FROM
FROM ${BUILD_FROM}

# ── JamBox APK repo ───────────────────────────────────────────────────────────
RUN echo "https://jambox-it.github.io/apk-repo/v3.23/JamBox" >> /etc/apk/repositories \
 && apk add --allow-untrusted jambox-keys \
 && apk update \
 && apk add --no-cache \
    py3-jb-mppsolar \
    py3-bluepy \
    py3-pymodbus \
    dropbear

# ── Web UI dependencies ───────────────────────────────────────────────────────
RUN pip3 install --no-cache-dir --break-system-packages \
    "flask>=3.0" \
    "zeroconf>=0.130"

# ── Common rootfs (synced from ../common/rootfs/ via `make sync`) ─────────────
# Build context is ha-mppsolar/ — rootfs/ is populated before committing.
COPY rootfs/ /

# ── HA-specific overrides (minimal — most things come from common rootfs) ─────
COPY root/ /

# ── Ensure scripts are executable ────────────────────────────────────────────
RUN chmod +x \
    /etc/services.d/web-ui/run \
    /etc/services.d/web-ui/finish \
    /etc/services.d/ssh-tunnel/run \
    /etc/services.d/ssh-tunnel/finish \
    /etc/services.d/inverter-manager/run \
    /etc/services.d/inverter-manager/finish \
    /opt/inverter-manager/manager.py \
    /opt/ssh-tunnel/manager.py \
    /opt/web-ui/app.py

# Persistent data in /data/ (HA addon convention)
ENV MPP_CONFIG_DIR=/data
