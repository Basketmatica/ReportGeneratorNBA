#!/usr/bin/env bash

# Instalar dependencias básicas
apt update && apt install -y wget curl unzip gnupg ca-certificates

# Instalar Node.js (Playwright lo necesita)
curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
apt install -y nodejs

# Instalar Playwright (usando npm)
npm install -g playwright

# Descargar los navegadores requeridos por Playwright
playwright install --with-deps

