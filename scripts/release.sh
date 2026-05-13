#!/usr/bin/env bash
# Manual PyPI release for proofagent-harness.
#
# Usage:
#   scripts/release.sh                  # build + upload to PyPI
#   scripts/release.sh --test           # build + upload to TestPyPI
#   scripts/release.sh --build-only     # build, sanity-check, no upload
#   scripts/release.sh --skip-checks    # skip pytest + ruff before building
#
# Prereqs (one-time):
#   pip install -e ".[dev]"             # gives you build + twine
#   Set up ~/.pypirc OR export TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-...
#
# Versioning is driven by hatch-vcs — it reads the latest git tag.
# Tag the commit you want to ship BEFORE running this script:
#   git tag v0.2.0 && git push --tags    # (or just `git tag v0.2.0` for local-only)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TARGET="pypi"
BUILD_ONLY=0
SKIP_CHECKS=0

for arg in "$@"; do
  case "$arg" in
    --test)        TARGET="testpypi" ;;
    --build-only)  BUILD_ONLY=1 ;;
    --skip-checks) SKIP_CHECKS=1 ;;
    -h|--help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown arg: $arg (try --help)" >&2
      exit 2
      ;;
  esac
done

# ── 0. Show what version will be built ───────────────────────────────────────
RAW_VERSION="$(python -c 'from hatchling.metadata.core import ProjectMetadata; import tomllib, pathlib; print("ok")' 2>/dev/null || echo "")"
if ! command -v python >/dev/null; then
  echo "python not found in PATH" >&2
  exit 1
fi

DETECTED_VERSION="$(python -m hatchling version 2>/dev/null || echo "unknown")"
echo "▶ Building version: $DETECTED_VERSION"
echo "▶ Target:           $TARGET"
echo

# Warn if working tree dirty — don't block (you might want a release of WIP).
if [ -n "$(git status --porcelain 2>/dev/null || true)" ]; then
  echo "⚠  working tree has uncommitted changes — proceeding anyway"
  echo
fi

# ── 1. Optional pre-flight checks ────────────────────────────────────────────
if [ "$SKIP_CHECKS" -eq 0 ]; then
  echo "▶ Running ruff + pytest (use --skip-checks to bypass)"
  ruff check src tests
  pytest tests/ -q
  echo
fi

# ── 2. Clean & build ─────────────────────────────────────────────────────────
echo "▶ Cleaning dist/"
rm -rf dist/ build/ src/*.egg-info

echo "▶ Building wheel + sdist"
python -m build

# ── 3. Sanity-check the wheel includes bundled data ──────────────────────────
echo "▶ Verifying wheel contains bundled traps / skills / personas"
python - <<'PY'
import glob, zipfile, sys
whl = sorted(glob.glob("dist/*.whl"))[-1]
needed = (
    "proofagent_harness/data/traps",
    "proofagent_harness/data/skills",
    "proofagent_harness/data/personas",
)
with zipfile.ZipFile(whl) as z:
    names = z.namelist()
    missing = [n for n in needed if not any(x.startswith(n) for x in names)]
    if missing:
        print(f"FAIL — missing in wheel: {missing}", file=sys.stderr)
        sys.exit(1)
    print(f"OK — {whl} contains all bundled data")
PY

# ── 4. Twine validation ──────────────────────────────────────────────────────
echo "▶ Running twine check"
twine check dist/*

if [ "$BUILD_ONLY" -eq 1 ]; then
  echo
  echo "✓ Build complete — skipping upload (--build-only)."
  echo "  Artifacts in dist/:"
  ls -lh dist/
  exit 0
fi

# ── 5. Upload ────────────────────────────────────────────────────────────────
echo
echo "▶ Uploading to $TARGET"
echo "  (twine will prompt for credentials if ~/.pypirc / TWINE_* env are unset)"
echo

if [ "$TARGET" = "testpypi" ]; then
  twine upload --repository testpypi dist/*
  echo
  echo "✓ Uploaded to TestPyPI."
  echo "  Smoke-test:"
  echo "    pip install -i https://test.pypi.org/simple/ proofagent-harness==$DETECTED_VERSION"
else
  twine upload dist/*
  echo
  echo "✓ Uploaded to PyPI."
  echo "  Verify:"
  echo "    pip install proofagent-harness==$DETECTED_VERSION"
fi
