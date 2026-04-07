ARG ALPINE_VERSION=3.23
FROM alpine:${ALPINE_VERSION}

ARG S6_OVERLAY_VERSION=3.2.0.0

# ── Base packages ─────────────────────────────────────────────────────────────
RUN apk add --no-cache \
    python3 \
    bash \
    wget \
    xz \
    tzdata \
    dropbear

# ── s6-overlay ────────────────────────────────────────────────────────────────
RUN ARCH=$(uname -m) \
 && wget -qO /tmp/s6-noarch.tar.xz \
      "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
 && wget -qO /tmp/s6-arch.tar.xz \
      "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${ARCH}.tar.xz" \
 && tar -C / -Jxpf /tmp/s6-noarch.tar.xz \
 && tar -C / -Jxpf /tmp/s6-arch.tar.xz \
 && rm /tmp/s6-*.tar.xz

# ── JamBox APK repo ───────────────────────────────────────────────────────────
# Add repo first, bootstrap the signing key with --allow-untrusted (keys pkg only),
# then install everything else with normal signature verification.
RUN echo "https://jambox-it.github.io/apk-repo/v3.23/JamBox" >> /etc/apk/repositories \
 && apk add --allow-untrusted jambox-keys \
 && apk update \
 && apk add --no-cache \
    py3-jb-mppsolar \
    py3-bluepy \
    py3-pymodbus \
    py3-pip \
 && pip3 install --no-cache-dir --break-system-packages \
    "flask>=3.0" \
    "zeroconf>=0.130" \
 && apk del py3-pip

# ── Common rootfs (synced from ../common/rootfs/ via `make sync`) ─────────────
# Build context is mppsolar-docker/ — rootfs/ is populated before committing.
COPY rootfs/ /

# ── Docker-specific overrides (mpp-install dev service, mppsolar longrun) ─────
COPY root/ /

# ── Ensure scripts are executable ────────────────────────────────────────────
RUN chmod +x \
    /etc/services.d/web-ui/run \
    /etc/services.d/web-ui/finish \
    /etc/services.d/ssh-tunnel/run \
    /etc/services.d/ssh-tunnel/finish \
    /etc/services.d/inverter-manager/run \
    /etc/services.d/inverter-manager/finish \
    /etc/s6-overlay/s6-rc.d/mpp-install/up \
    /etc/s6-overlay/s6-rc.d/mppsolar/run \
    /etc/s6-overlay/s6-rc.d/mppsolar/finish \
    /etc/s6-overlay/scripts/mpp-install.sh \
    /opt/inverter-manager/manager.py \
    /opt/ssh-tunnel/manager.py \
    /opt/web-ui/app.py

# Config lives on a mounted volume; /config/inverters/ created by cont-init
VOLUME ["/config"]

# Web UI port (override with WEB_UI_PORT env var)
EXPOSE 7669

ENTRYPOINT ["/init"]
