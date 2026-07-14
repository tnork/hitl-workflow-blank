#!/usr/bin/env bash
# Build a customer-ready zip of this repo.
#
#   ./package.sh                  -> ~/Desktop/hitl-review.zip
#   ./package.sh acme-review      -> ~/Desktop/acme-review.zip
#
# Excludes git history, caches, and anything machine-specific: the API key (.env),
# the session secret, the user database, and local settings. Resets the sample
# document to the inbox so the recipient opens to something awaiting review.
set -euo pipefail

NAME="${1:-hitl-review}"
SRC="$(cd "$(dirname "$0")" && pwd)"
OUT="$HOME/Desktop/${NAME}.zip"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

rsync -a \
  --exclude '.git' --exclude '.gitignore' \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.pytest_cache' \
  --exclude '.venv' --exclude 'venv' \
  --exclude '.env' --exclude '.secret_key' \
  --exclude 'users.json' --exclude 'settings.json' \
  --exclude '.DS_Store' \
  --exclude 'package.sh' \
  "$SRC/" "$STAGE/$NAME/"

# Any document already signed off goes back to the inbox, so the demo starts fresh.
python3 - "$STAGE/$NAME" <<'PY'
import shutil, sys
from pathlib import Path
root = Path(sys.argv[1])
inbox, outbox = root / "docs_inbox", root / "docs_outbox"
inbox.mkdir(exist_ok=True); outbox.mkdir(exist_ok=True)
for f in outbox.glob("*"):
    if f.is_file() and f.name != ".gitkeep":
        shutil.move(str(f), str(inbox / f.name))
PY

rm -f "$OUT"
( cd "$STAGE" && zip -qr "$OUT" "$NAME" -x '*.DS_Store' '*__pycache__*' )

echo "→ $OUT  ($(du -h "$OUT" | cut -f1))"
echo "  Recipient runs:  pip install -r requirements.txt && python3 web_app.py"
