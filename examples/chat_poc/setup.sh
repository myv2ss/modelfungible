#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# ModelFungible Chat POC — Setup script
# Run this once to install dependencies, then start the app with: python3 app.py
# ─────────────────────────────────────────────────────────────────────────────

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  ModelFungible Chat POC — Setup"
echo "═══════════════════════════════════════════════════════"
echo ""

# Check Python
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 not found. Please install Python 3.10+."
    exit 1
fi
echo "✓ Python: $(python3 --version)"

# Create .env from example if it doesn't exist
if [ ! -f .env ]; then
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "✓ Created .env from .env.example"
        echo ""
        echo "⚠  Edit .env and add your GROQ_API_KEY (or other API keys)!"
        echo "   Get a free key at https://console.groq.com/keys"
    else
        echo "⚠  No .env.example found — skipping .env creation"
    fi
else
    echo "✓ .env already exists"
fi

# Set up virtual environment
if [ ! -d "venv" ]; then
    echo ""
    echo "→ Creating virtual environment..."
    python3 -m venv venv
    echo "✓ venv created at: $SCRIPT_DIR/venv"
else
    echo "✓ venv already exists"
fi

# Activate venv and install dependencies
echo ""
echo "→ Installing dependencies..."
source venv/bin/activate
pip install --quiet flask python-dotenv
echo "✓ Dependencies installed (flask, python-dotenv)"
echo ""
echo "✓ Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your GROQ_API_KEY"
echo "  2. Activate the venv:  source venv/bin/activate"
echo "  3. Start the app:       python3 app.py"
echo "  4. Open:                http://localhost:8766"
echo ""
echo "To activate the venv in the future:"
echo "  source \"$SCRIPT_DIR/venv/bin/activate\""
echo ""
