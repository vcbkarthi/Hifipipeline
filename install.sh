#!/bin/bash
set -e

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  HiFi Pipeline — Dependency Installer"
echo "  macOS / Apple Silicon (M1 16GB)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Homebrew
if ! command -v brew &>/dev/null; then
  echo "→ Installing Homebrew..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# FFmpeg (with VideoToolbox for Apple Silicon HW encode)
echo "→ Installing ffmpeg..."
brew install ffmpeg

# Ollama (local LLM runner)
echo "→ Installing Ollama..."
brew install ollama

# Python deps
echo "→ Installing Python packages..."
pip3 install -r requirements.txt
pip3 install flask keyring google-api-python-client google-auth-httplib2 google-auth-oauthlib

# Pull LLaVA vision model (~4GB — runs comfortably on M1 16GB)
echo "→ Pulling LLaVA model via Ollama (this downloads ~4GB once)..."
ollama pull llava

# Create assets folder placeholder
mkdir -p assets output/shorts output/longform output/transcripts output/manifests output/frames

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ All done!"
echo ""
echo "  Next: drop your channel logo at assets/logo.png"
echo "        (PNG with transparency recommended)"
echo ""
echo "  Then run:"
echo '  python pipeline.py --input /Volumes/YourSSD/ShowFolder --show "Show Name 2026"'
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
