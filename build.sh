#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build.sh — Script de build para Render.com
#
# Render ejecuta este script ANTES de pip install -r requirements.txt
# (lo encadenamos en render.yaml). Aquí solo instalamos las dependencias
# nativas que necesita WeasyPrint para renderizar PDF; lo de pip lo hace
# Render por su cuenta.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "==> Instalando dependencias del sistema para WeasyPrint…"

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation

echo "==> Dependencias del sistema instaladas correctamente."
