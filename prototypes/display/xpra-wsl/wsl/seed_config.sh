#!/bin/bash
# Seed a sandboxed KiCad config root in WSL and a scratch copy of the ecc83 demo. Copy of the kiwrap.sandbox
# recipe (docs/VERIFICATION.md claim 10): kicad_common.json with api.enable_server + the do_not_show_again flags
# + system.first_run_shown, plus the three lib tables from /usr/share/kicad/template. Never touches ~/.config/kicad.
# NOTE: running any kicad binary WITHOUT these env vars creates ~/.config/kicad/10.0 (verified with
# `kicad-cli version`) - always `source $HOME/spike/env.sh` first.
#   usage: bash seed_config.sh [ROOT]     (default ROOT=$HOME/spike/kicad-sandbox; env file at $HOME/spike/env.sh)
set -euo pipefail
ROOT=${1:-$HOME/spike/kicad-sandbox}
mkdir -p "$ROOT/config/10.0" "$ROOT/documents" "$ROOT/cache" "$ROOT/work"
cat > "$ROOT/config/10.0/kicad_common.json" <<'JSON'
{
  "api": {"enable_server": false, "interpreter_path": "/usr/bin/python3"},
  "do_not_show_again": {
    "data_collection_prompt": true,
    "update_check_prompt": true,
    "env_var_overwrite_warning": true,
    "scaled_3d_models_warning": true,
    "zone_fill_warning": true
  },
  "environment": {"vars": null},
  "system": {"first_run_shown": true}
}
JSON
for t in sym-lib-table fp-lib-table design-block-lib-table; do
  if [ -f "/usr/share/kicad/template/$t" ]; then cp "/usr/share/kicad/template/$t" "$ROOT/config/10.0/$t"; fi
done
if [ ! -f "$ROOT/config/10.0/design-block-lib-table" ]; then
  echo '(design_block_lib_table (version 7))' > "$ROOT/config/10.0/design-block-lib-table"
fi
rm -rf "$ROOT/work/ecc83"
cp -r /usr/share/kicad/demos/ecc83 "$ROOT/work/ecc83"
ENVFILE="$(dirname "$ROOT")/env.sh"
cat > "$ENVFILE" <<ENV
export KICAD_CONFIG_HOME=$ROOT/config
export KICAD_DOCUMENTS_HOME=$ROOT/documents
export KICAD_CACHE_HOME=$ROOT/cache
export TMPDIR=/tmp
export PCB=$ROOT/work/ecc83/ecc83-pp.kicad_pcb
export SCH=$ROOT/work/ecc83/ecc83-pp.kicad_sch
ENV
echo "seeded $ROOT; run: source $ENVFILE"
