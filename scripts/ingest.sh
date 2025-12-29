#!/bin/bash
# Ingestion script - builds the vector database from documentation and examples
# Run this locally, then copy ./data/chroma/* to server

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Error: Virtual environment not found!"
    echo "Run: python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# Activate venv
source venv/bin/activate

echo "=============================================="
echo "Nocode AI - Document Ingestion"
echo "=============================================="
echo ""
echo "This will ingest documents from:"
echo "  - AI Context docs: ${AICONTEXT_PATH:-../nocode-ui/ui-app/aicontext}"
echo "  - App definitions: ${APP_DEFINITIONS_PATH:-./definitions/app defs}"
echo "  - Site definitions: ${SITE_DEFINITIONS_PATH:-./definitions/site defs}"
echo ""
echo "ChromaDB will be stored in: ${CHROMA_PERSIST_DIR:-./data/chroma}"
echo ""

# Run ingestion
python scripts/ingest.py

echo ""
echo "=============================================="
echo "Ingestion Complete!"
echo "=============================================="
echo ""
echo "To deploy to dev server, copy the chroma data:"
echo "  scp -r ./data/chroma/* opc@dev-server:/var/data/ai/chroma/"
echo ""
