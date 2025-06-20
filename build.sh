#!/usr/bin/env bash

# Actualizar paquetes del sistema
apt update && apt install -y wget curl unzip gnupg ca-certificates

# Instalar Python y pip si no están disponibles
apt install -y python3 python3-pip

# Instalar Playwright para Python
pip install --upgrade pip
pip install playwright

# Descargar los navegadores y sus dependencias del sistema
playwright install --with-deps

