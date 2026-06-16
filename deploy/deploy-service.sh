#!/usr/bin/env bash
# On-demand image rollout for swap, invoked by CI over SSH right after it pushes a
# new image (see .github/workflows/deploy.yml). This replaces the old Watchtower
# poll/nudge: the image is pulled once, on release, instead of polling the registry
# on a timer — no rollout latency and no inbound HTTP deploy channel. Mirrors the
# emercoin_docker stack's deploy-service.sh (the sibling pattern this repo follows).
#
#   bash /opt/swap/deploy/deploy-service.sh
#
# Hardening: swap is a single service, so this takes no argument — pin the CI key to
# exactly this command in ~/.ssh/authorized_keys, e.g.
#   command="/opt/swap/deploy/deploy-service.sh",no-pty,no-port-forwarding ssh-ed25519 AAAA... ci-deploy
set -euo pipefail

REPO=/opt/swap
cd "$REPO"
git pull --ff-only                      # pick up any compose/.env/site shipped with the release
COMPOSE=(docker compose -f deploy/docker-compose.droplet.yaml --env-file deploy/.env)

# The image is public on GHCR, so `compose pull` needs no `docker login`.
"${COMPOSE[@]}" pull swap
"${COMPOSE[@]}" up -d swap
docker image prune -f
echo "deployed swap ($("${COMPOSE[@]}" images -q swap | cut -c1-12))"
