#!/usr/bin/env bash
# kiwrap developer/user bootstrap for Linux (bash >= 4; needs curl, tar, sha256sum).
#
# Idempotent. Ensures, in this order:
#   1. uv (uses the one on PATH; otherwise installs a private copy under <storage>/uv)
#   2. `uv sync --all-extras` in the repo root
#   3. a private Eclipse Temurin JRE 25 under <storage>/engines/jre/     (pinned in scripts/engines.json)
#   4. freerouting-2.4.1.jar under <storage>/engines/freerouting/          (pinned in scripts/engines.json)
#   5. a Freerouting profile under <storage>/freerouting/freerouting.json with analytics, telemetry,
#      contact and the GitHub update check all OFF
#   6. reports Node.js (LTS lines are even majors; the VS Code extension needs it in Phase 3)
#   7. reports the KiCad installs `kiwrap.installation` (or a PATH/apt/flatpak glob) finds
#
# <storage> is $KIWRAP_HOME if set, else ${XDG_DATA_HOME:-~/.local/share}/kiwrap (same rule as kiwrap.platform).
# Every download is verified against the sha256 pinned in engines.json before it is used.
# Nothing is written to the user's ~/.config/kicad: every kicad-cli call runs with KICAD_CONFIG_HOME pointed at
# a throw-away directory, and the script fingerprints ~/.config/kicad before and after to prove it.
#
# Usage:  bash scripts/bootstrap.sh [--check] [--skip-sync] [--storage-home DIR]
#   --check       report only (verifies hashes, downloads nothing); exit 0 = all present, 2 = something missing
#   --skip-sync   do not run `uv sync`
#
# JSON handling (engines.json, the Freerouting profile) is done with python3: the system one when present,
# otherwise the interpreter uv provides (`uv run --no-project`), so no extra packages are needed.
set -euo pipefail

CHECK=0
SKIP_SYNC=0
STORAGE_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --skip-sync) SKIP_SYNC=1 ;;
    --storage-home) shift; STORAGE_OVERRIDE="${1:-}" ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
  shift
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PINS="$SCRIPT_DIR/engines.json"

if [ -n "$STORAGE_OVERRIDE" ]; then STORAGE="$STORAGE_OVERRIDE"
elif [ -n "${KIWRAP_HOME:-}" ]; then STORAGE="$KIWRAP_HOME"
else STORAGE="${XDG_DATA_HOME:-$HOME/.local/share}/kiwrap"; fi
ENGINES="$STORAGE/engines"
DOWNLOADS="$ENGINES/downloads"
JRE_DIR="$ENGINES/jre"
FR_DIR="$ENGINES/freerouting"
PROFILE_DIR="$STORAGE/freerouting"
UV_DIR="$STORAGE/uv"

FAILURES=()
MISSING=()
ROWS=()

status() { # status <item> <ok|missing|warn|fail> <detail>
  local tag; tag="$(printf '%-7s' "$(echo "$2" | tr '[:lower:]' '[:upper:]')")"
  echo "[$tag] $1: $3"
  ROWS+=("$(printf '%-8s %s' "$2" "$1")")
  case "$2" in fail) FAILURES+=("$1") ;; missing) MISSING+=("$1") ;; esac
}

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# ------------------------------------------------------------------------------------------------ python for JSON

PY=""
find_python() {
  if [ -n "$PY" ]; then return 0; fi
  if command -v python3 >/dev/null 2>&1; then PY="python3"; return 0; fi
  if [ -n "${UV:-}" ]; then PY="$UV run --no-project --python 3.11 python"; return 0; fi
  return 1
}
pyjson() { # pyjson <expr over `d` (parsed engines.json)>
  find_python || { echo "no python3 and no uv to read $PINS" >&2; return 1; }
  # shellcheck disable=SC2086
  $PY -c "import json,sys; d=json.load(open(sys.argv[1])); print($1)" "$PINS"
}

# ------------------------------------------------------------------------------------------------ 1. uv

UV=""
if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"
elif [ -x "$UV_DIR/uv" ]; then UV="$UV_DIR/uv"; fi
if [ -z "$UV" ] && [ "$CHECK" -eq 0 ]; then
  echo "  installing uv into $UV_DIR (official installer, PATH left untouched)"
  mkdir -p "$UV_DIR"
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$UV_DIR" UV_NO_MODIFY_PATH=1 sh
  [ -x "$UV_DIR/uv" ] && UV="$UV_DIR/uv"
fi
MODE=install; [ "$CHECK" -eq 1 ] && MODE=check
echo "kiwrap bootstrap ($MODE) - storage home: $STORAGE"
echo "  pins: JRE $(pyjson "d['jre']['release_name']") / Freerouting $(pyjson "d['freerouting']['version']") (scripts/engines.json, pinned $(pyjson "d['pinned_on']"))"
if [ "$CHECK" -eq 0 ]; then mkdir -p "$STORAGE" "$ENGINES" "$DOWNLOADS" "$JRE_DIR" "$FR_DIR" "$PROFILE_DIR"; fi
if [ -z "$UV" ]; then status uv missing "not on PATH and not under $UV_DIR"
else status uv ok "$("$UV" --version) ($UV)"; fi

# ------------------------------------------------------------------------------------------------ 2. uv sync

VENV_PY="$REPO_ROOT/.venv/bin/python"
if [ -n "$UV" ] && [ "$CHECK" -eq 0 ] && [ "$SKIP_SYNC" -eq 0 ]; then
  echo "  uv sync --all-extras (repo: $REPO_ROOT)"
  if (cd "$REPO_ROOT" && "$UV" sync --all-extras); then status venv ok "uv sync --all-extras done ($VENV_PY)"
  else status venv fail "uv sync --all-extras failed"; fi
elif [ -x "$VENV_PY" ]; then status venv ok "$VENV_PY"
else status venv missing "$VENV_PY (run without --check to create it)"; fi

# ------------------------------------------------------------------------------------------------ helpers

download_verified() { # download_verified <url> <dest> <sha256> <size>
  local url="$1" dest="$2" want="$3" size="$4"
  if [ -f "$dest" ]; then
    if [ "$(sha256_of "$dest")" = "$want" ]; then return 0; fi
    echo "  cached file has a different hash; re-downloading $dest"; rm -f "$dest"
  fi
  mkdir -p "$(dirname "$dest")"
  echo "  downloading $url ($((size / 1048576)) MB)"
  curl -L --fail --silent --show-error --retry 3 -o "$dest.part" "$url"
  local got; got="$(sha256_of "$dest.part")"
  if [ "$got" != "$want" ]; then
    rm -f "$dest.part"
    echo "sha256 mismatch for $url: expected $want, got $got" >&2
    return 1
  fi
  mv -f "$dest.part" "$dest"
}

# ------------------------------------------------------------------------------------------------ 3. JRE 25

JRE_URL="$(pyjson "d['jre']['linux']['url']")"
JRE_ASSET="$(pyjson "d['jre']['linux']['asset']")"
JRE_SHA="$(pyjson "d['jre']['linux']['sha256']")"
JRE_SIZE="$(pyjson "d['jre']['linux']['size']")"
JRE_TOP="$(pyjson "d['jre']['linux']['top_dir']")"
JAVA="$JRE_DIR/$JRE_TOP/$(pyjson "d['jre']['linux']['java']")"
JRE_MARKER="$JRE_DIR/installed.sha256"
jre_ok=0
if [ -x "$JAVA" ] && [ -f "$JRE_MARKER" ] && [ "$(cat "$JRE_MARKER")" = "$JRE_SHA" ]; then jre_ok=1; fi
if [ "$jre_ok" -eq 0 ] && [ "$CHECK" -eq 0 ]; then
  if download_verified "$JRE_URL" "$DOWNLOADS/$JRE_ASSET" "$JRE_SHA" "$JRE_SIZE"; then
    rm -rf "${JRE_DIR:?}/$JRE_TOP"
    echo "  extracting $JRE_ASSET -> $JRE_DIR"
    tar -xzf "$DOWNLOADS/$JRE_ASSET" -C "$JRE_DIR"
    if [ -x "$JAVA" ]; then echo "$JRE_SHA" > "$JRE_MARKER"; jre_ok=1
    else status jre fail "archive did not contain $JRE_TOP/bin/java"; fi
  else
    status jre fail "download/verification failed"
  fi
fi
if [ "$jre_ok" -eq 1 ]; then status jre ok "$("$JAVA" -version 2>&1 | head -n1) ($JAVA; archive sha256 $JRE_SHA)"
elif [ "$CHECK" -eq 1 ]; then status jre missing "$JAVA (or marker hash != pinned)"; fi

# ------------------------------------------------------------------------------------------------ 4. Freerouting jar

FR_URL="$(pyjson "d['freerouting']['url']")"
FR_ASSET="$(pyjson "d['freerouting']['asset']")"
FR_SHA="$(pyjson "d['freerouting']['sha256']")"
FR_SIZE="$(pyjson "d['freerouting']['size']")"
FR_VERSION="$(pyjson "d['freerouting']['version']")"
JAR="$FR_DIR/$FR_ASSET"
jar_ok=0
if [ -f "$JAR" ]; then
  h="$(sha256_of "$JAR")"
  if [ "$h" = "$FR_SHA" ]; then jar_ok=1
  elif [ "$CHECK" -eq 0 ]; then echo "  $JAR has sha256 $h, expected $FR_SHA; replacing"; rm -f "$JAR"
  else status freerouting fail "sha256 mismatch: $h != $FR_SHA"; fi
fi
if [ "$jar_ok" -eq 0 ] && [ "$CHECK" -eq 0 ]; then
  if download_verified "$FR_URL" "$JAR" "$FR_SHA" "$FR_SIZE"; then jar_ok=1
  else status freerouting fail "download/verification failed"; fi
fi
if [ "$jar_ok" -eq 1 ]; then status freerouting ok "$JAR (sha256 $FR_SHA verified)"
elif [ "$CHECK" -eq 1 ] && [ ! -f "$JAR" ]; then status freerouting missing "$JAR"; fi

# ------------------------------------------------------------------------------------------------ 5. Freerouting profile

# Keys and values: profile written by Freerouting 2.4.1 itself (spikes/autorouting/fr_userdata/freerouting.json),
# docs/research/04-autorouting.md, ARCHITECTURE.md section 8. The GitHub version check
# (app.freerouting.util.VersionChecker, started unconditionally from main in 2.4.1 - no settings key exists) is
# blackholed through network.proxy_url -> https.proxyHost, which the JVM default HttpClient honours;
# localhost/127.0.0.1 stay on the non-proxy list so the local REST/MCP servers are unaffected.
PROFILE="$PROFILE_DIR/freerouting.json"
PROFILE_PY='
import json, sys, uuid
path, mode, version, log_path = sys.argv[1:5]
enforced = {
    "version": version,
    "profile.email": "",
    "profile.allow_telemetry": False,
    "profile.allow_contact": False,
    "usage_and_diagnostic_data.disable_analytics": True,
    "usage_and_diagnostic_data.track_window_changed": False,
    "usage_and_diagnostic_data.track_button_clicked": False,
    "gui.enabled": False,
    "gui.show_routing_summary": False,
    "feature_flags.save_jobs": False,
    "api_server.enabled": False,
    "api_server.http_allowed": True,
    "api_server.endpoints": ["http://127.0.0.1:37864"],
    "mcp_server.enabled": False,
    "network.proxy_url": "http://127.0.0.1:9",
    "network.no_proxy": "localhost,127.0.0.1",
    "logging.console.enabled": True,
    "logging.file.enabled": True,
    "logging.file.level": "INFO",
    "logging.file.location": log_path,
}
def get(d, key):
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d
def put(d, key, value):
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(d.get(part), dict):
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError):
    data = {}
if mode == "write":
    if not get(data, "profile.id"):
        put(data, "profile.id", str(uuid.uuid4()))
    for k, v in enforced.items():
        put(data, k, v)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
bad = [f"{k}={get(data, k)!r} (want {v!r})" for k, v in enforced.items() if get(data, k) != v]
print("; ".join(bad))
sys.exit(1 if bad else 0)
'
profile_problems=""
if find_python; then
  if [ "$CHECK" -eq 0 ]; then
    # shellcheck disable=SC2086
    profile_problems="$($PY -c "$PROFILE_PY" "$PROFILE" write "$FR_VERSION" "$PROFILE_DIR/freerouting.log")" || true
  elif [ -f "$PROFILE" ]; then
    # shellcheck disable=SC2086
    profile_problems="$($PY -c "$PROFILE_PY" "$PROFILE" check "$FR_VERSION" "$PROFILE_DIR/freerouting.log")" || true
  fi
  if [ ! -f "$PROFILE" ]; then status freerouting-profile missing "$PROFILE"
  elif [ -z "$profile_problems" ]; then status freerouting-profile ok "$PROFILE (analytics/telemetry/contact/update-check off)"
  else status freerouting-profile fail "$profile_problems"; fi
else
  status freerouting-profile warn "no python3/uv available to write or verify $PROFILE"
fi

# Smoke test: the jar must start under the private JRE with this profile; re-validate the profile afterwards.
if [ "$jre_ok" -eq 1 ] && [ "$jar_ok" -eq 1 ] && [ "$CHECK" -eq 0 ] && find_python; then
  banner="$( (cd "$PROFILE_DIR" && timeout 120 "$JAVA" -jar "$JAR" "--user_data_path=$PROFILE_DIR" --gui.enabled=false -help 2>&1) | grep -m1 'Freerouting v' || true)"
  # shellcheck disable=SC2086
  after="$($PY -c "$PROFILE_PY" "$PROFILE" check "$FR_VERSION" "$PROFILE_DIR/freerouting.log")" || true
  if [ -n "$banner" ] && [ -z "$after" ]; then status freerouting-run ok "$banner; profile keys intact after the run"
  elif [ -z "$banner" ]; then status freerouting-run fail "no 'Freerouting v' banner from -help (JRE/jar broken?)"
  else status freerouting-run fail "profile changed by Freerouting: $after"; fi
fi

# ------------------------------------------------------------------------------------------------ 6. Node (report only)

if command -v node >/dev/null 2>&1; then
  nv="$(node --version)"
  major="${nv#v}"; major="${major%%.*}"
  if [ "$major" -ge 20 ] && [ $((major % 2)) -eq 0 ]; then status node ok "$nv ($(command -v node)) - even major = LTS line"
  else status node warn "$nv is not an LTS line (even majors >= 20 are); report only"; fi
else
  status node warn "not on PATH - install Node LTS (https://nodejs.org) before Phase 3 (VS Code extension); report only"
fi

# ------------------------------------------------------------------------------------------------ 7. KiCad installs (report only)

USER_KICAD="${XDG_CONFIG_HOME:-$HOME/.config}/kicad"
fingerprint() { # path|size|mtime|sha256 of every file, hashed (mirrors Get-DirectoryFingerprint in bootstrap.ps1)
  if [ ! -d "$1" ]; then echo absent; return; fi
  (cd "$1" && find . -type f -print0 | sort -z | xargs -0 -r stat -c '%n|%s|%Y' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) | sha256sum | cut -d' ' -f1
}
fp_before="$(fingerprint "$USER_KICAD")"
THROWAWAY="$(mktemp -d "${TMPDIR:-/tmp}/kiwrap-bootstrap.XXXXXX")"
# Hard rule: every kicad-cli invocation (even `version`) runs with KICAD_CONFIG_HOME pointed away from the user's
# config because kicad-cli rewrites kicad_common.json on exit (ARCHITECTURE.md section 6).
export KICAD_CONFIG_HOME="$THROWAWAY/config" KICAD_DOCUMENTS_HOME="$THROWAWAY/documents" KICAD_CACHE_HOME="$THROWAWAY/cache"
export KIWRAP_HOME="$STORAGE"
reported=0
if [ -n "$UV" ] && [ -x "$VENV_PY" ]; then
  for extra in "" "--no-probe"; do
    # shellcheck disable=SC2086
    if out="$(cd "$REPO_ROOT" && "$UV" run --no-sync python -m kiwrap.installation $extra 2>/dev/null)" && [ -n "$out" ]; then
      status kicad ok "via \`uv run python -m kiwrap.installation $extra\`:"
      while IFS= read -r line; do echo "         $line"; done <<< "$out"
      reported=1; break
    fi
  done
fi
if [ "$reported" -eq 0 ]; then
  clis=()
  for c in /usr/bin/kicad-cli /usr/local/bin/kicad-cli /opt/kicad*/bin/kicad-cli /snap/bin/kicad-cli; do [ -x "$c" ] && clis+=("$c"); done
  if command -v kicad-cli >/dev/null 2>&1; then
    p="$(command -v kicad-cli)"; found=0
    for c in "${clis[@]:-}"; do [ "$c" = "$p" ] && found=1; done
    [ "$found" -eq 0 ] && clis+=("$p")
  fi
  if command -v flatpak >/dev/null 2>&1 && flatpak info org.kicad.KiCad >/dev/null 2>&1; then
    status kicad-flatpak ok "org.kicad.KiCad installed ($(flatpak info org.kicad.KiCad 2>/dev/null | grep -m1 -i '^ *Version' | tr -s ' '))"
  fi
  if [ "${#clis[@]}" -eq 0 ]; then
    status kicad warn "no kicad-cli under /usr, /usr/local, /opt/kicad*, /snap/bin or on PATH (kiwrap.installation not usable yet); report only"
  else
    status kicad ok "${#clis[@]} install(s) found by glob (kiwrap.installation not usable yet):"
    for c in "${clis[@]}"; do echo "         $c  ->  $("$c" version --format plain 2>&1 | head -n1)"; done
  fi
fi
rm -rf "$THROWAWAY"
fp_after="$(fingerprint "$USER_KICAD")"
if [ "$fp_before" = "$fp_after" ]; then status user-kicad-config ok "$USER_KICAD untouched (fingerprint ${fp_before:0:12})"
else status user-kicad-config fail "$USER_KICAD CHANGED during bootstrap (before $fp_before, after $fp_after)"; fi

# ------------------------------------------------------------------------------------------------ summary

echo
echo "Summary ($MODE):"
for r in "${ROWS[@]}"; do echo "  $r"; done
if [ "${#FAILURES[@]}" -gt 0 ]; then echo "FAILED: ${FAILURES[*]}"; exit 1; fi
if [ "${#MISSING[@]}" -gt 0 ]; then echo "MISSING: ${MISSING[*]}"; exit 2; fi
exit 0
