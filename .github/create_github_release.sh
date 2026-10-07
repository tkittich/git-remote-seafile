#!/usr/bin/env bash
#
# Create the GitHub Release for a tag, in this project's established format.
#
# Why this exists: a GitHub Release is a *separate object* from its git tag, so
# pushing a tag used to leave a bare tag behind -- v0.3.0 ended up tagged with
# no release at all, while v0.2.0 and v0.2.1 have releases because they were
# made by hand.  Running this from the tag-triggered workflow makes the release
# automatic, using the workflow's own token, so no personal token is involved.
#
# The output mirrors the hand-made releases exactly:
#
#   title   "vX.Y.Z - <summary>", taken from the notes file's first line
#   body    "## What's Changed in vX.Y.Z" onwards -- the rest of that file
#   assets  the built wheel and sdist from $DIST_DIR
#   tail    "---" then a "**Full Changelog**" compare link (part of the notes)
#
# Usage: create_github_release.sh <tag>
#
# Environment:
#   NOTES_DIR  where <tag>.md release bodies live (default .github/release-notes)
#   DIST_DIR   build artifacts to attach (default dist)
#
# Needs `gh` on PATH with a token allowed to write releases.
set -euo pipefail

NOTES_DIR="${NOTES_DIR:-.github/release-notes}"
DIST_DIR="${DIST_DIR:-dist}"

tag="${1:-}"
if [ -z "$tag" ]; then
  echo "usage: $(basename "$0") <tag>" >&2
  exit 2
fi

notes_file="$NOTES_DIR/$tag.md"
if [ -f "$notes_file" ]; then
  # The first line is the release *name*; everything after it is the body.  The
  # hand-made releases keep the title out of the body, so drop that line along
  # with the blank line that follows it.
  title="$(head -n 1 "$notes_file" | sed -e 's/^#[[:space:]]*//')"
  body_file="$(mktemp)"
  trap 'rm -f "$body_file"' EXIT
  tail -n +2 "$notes_file" | awk 'NF { found = 1 } found' > "$body_file"
  notes_args=(--notes-file "$body_file")
  echo "Using the curated notes in $notes_file"
else
  title="$tag"
  notes_args=(--generate-notes)
  echo "No $notes_file; falling back to generated notes"
fi

# Idempotent on purpose: re-running the workflow, or a tag moved after a failed
# run, must not clobber a release that is already published.
if gh release view "$tag" >/dev/null 2>&1; then
  echo "Release $tag already exists; leaving it untouched."
  exit 0
fi

# Attach whatever was built.  An unmatched glob would be passed through
# literally and make gh fail, so collect the files first and only pass them if
# there are any.
assets=()
if [ -d "$DIST_DIR" ]; then
  for candidate in "$DIST_DIR"/*; do
    if [ -e "$candidate" ]; then
      assets+=("$candidate")
    fi
  done
fi

echo "Creating release $tag with title: $title"
if [ "${#assets[@]}" -gt 0 ]; then
  gh release create "$tag" --title "$title" "${notes_args[@]}" "${assets[@]}"
else
  gh release create "$tag" --title "$title" "${notes_args[@]}"
fi
