#!/usr/bin/env bash
# Build script para Render.com

# Instalar dependencias del sistema para WeasyPrint (renderizado PDF)
apt-get update -qq
apt-get install -y -qq \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf2.0-0 \
    libffi-dev \
    shared-mime-info

# Instalar dependencias Python
pip install --upgrade pip
pip install -r requirements.txt
