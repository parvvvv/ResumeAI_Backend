#!/usr/bin/env bash
# Exit on error
set -o errexit

# Upgrade pip
python -m pip install --upgrade pip

# Install python dependencies
pip install -r requirements.txt

# Install Playwright Chromium with OS dependencies
playwright install --with-deps chromium
