#!/bin/sh
set -eu
AGENTNEXUS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTNEXUS_SOURCE=${AGENTNEXUS_DATA_DIR:-"$AGENTNEXUS_ROOT/data"}
AGENTNEXUS_BACKUP_ROOT=${AGENTNEXUS_BACKUP_DIR:-"$AGENTNEXUS_ROOT/backups"}
AGENTNEXUS_STAMP=$(date -u +%Y%m%dT%H%M%SZ)
exec python3 "$AGENTNEXUS_ROOT/scripts/data_snapshot.py" backup "$AGENTNEXUS_SOURCE" "$AGENTNEXUS_BACKUP_ROOT/agentnexus-$AGENTNEXUS_STAMP-$$" "$@"
