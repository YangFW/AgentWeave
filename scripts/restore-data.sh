#!/bin/sh
set -eu
AGENTNEXUS_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
AGENTNEXUS_SNAPSHOT=${1:?用法：restore-data.sh 备份目录 新恢复目录}
AGENTNEXUS_RESTORED=${2:?必须指定不存在的新目录，不覆盖当前 data}
exec python3 "$AGENTNEXUS_ROOT/scripts/data_snapshot.py" restore "$AGENTNEXUS_SNAPSHOT" "$AGENTNEXUS_RESTORED"
