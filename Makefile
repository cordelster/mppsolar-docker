.PHONY: sync build up down logs

# ── Sync common rootfs into the HA addon directory ────────────────────────────
# Run this whenever common/rootfs/ changes, then commit ha-mppsolar/rootfs/.
# CI builds from the committed copy — no sync step needed in the workflow.
sync:
	rsync -av --delete common/rootfs/ ha-mppsolar/rootfs/

# ── Standalone Docker ─────────────────────────────────────────────────────────
build:
	cd mppsolar-docker && docker compose build --no-cache

up:
	cd mppsolar-docker && docker compose up -d

down:
	cd mppsolar-docker && docker compose down

logs:
	cd mppsolar-docker && docker compose logs -f

# ── Local HA addon test build (amd64) ─────────────────────────────────────────
build-ha: sync
	docker build \
	  --build-arg BUILD_FROM=ghcr.io/home-assistant/amd64-base:3.23 \
	  -t local/mppsolar-ha:test \
	  ha-mppsolar/
