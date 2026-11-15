#!/usr/bin/env bash
# Private denylist check: fail if tracked files or commit metadata match any pattern.
#
#   scripts/check_leaks.sh FILE...    check the given files (pre-commit passes staged files)
#   scripts/check_leaks.sh --all      check every tracked file, all paths and the full git log
#   scripts/check_leaks.sh --email    check the configured commit author email only
#
# Patterns: one extended regex per line (case-insensitive, '#' starts a comment) in the
# file named by $PGVID_DENYLIST. Keep that file OUTSIDE this repository, since it lists
# exactly the strings that must never appear here.
# Optional: $PGVID_ALLOWED_EMAIL, a regex every author/committer email must match.
#
# Output never shows the matched text or the pattern itself (CI logs are public),
# only file:line and the pattern's line number in your denylist.
set -euo pipefail

mode="files"
case "${1:-}" in
  --all) mode="all"; shift ;;
  --email) mode="email"; shift ;;
esac

fail=0

check_email() {  # $1 = email, $2 = where
  if [[ -n "${PGVID_ALLOWED_EMAIL:-}" ]] && ! grep -qiE -- "$PGVID_ALLOWED_EMAIL" <<<"$1"; then
    echo "check_leaks: $2 email does not match PGVID_ALLOWED_EMAIL" >&2
    fail=1
  fi
}

if [[ "$mode" == "email" ]]; then
  check_email "$(git config user.email || true)" "configured author"
  exit $fail
fi

list="${PGVID_DENYLIST:-}"
if [[ -z "$list" || ! -f "$list" ]]; then
  echo "check_leaks: set PGVID_DENYLIST to your private pattern file (kept outside the repo)" >&2
  exit 2
fi

root="$(git rev-parse --show-toplevel)"
cd "$root"
list_abs="$(cd "$(dirname "$list")" && pwd)/$(basename "$list")"
case "$list_abs" in
  "$root"/*) echo "check_leaks: the denylist must live outside the repository" >&2; exit 2 ;;
esac

files=()
if [[ "$mode" == "all" ]]; then
  while IFS= read -r f; do files+=("$f"); done < <(git ls-files)   # tracked/staged only: CI tools drop untracked reports (e.g. results.sarif)
else
  for f in "$@"; do [[ -f "$f" ]] && files+=("$f"); done
fi

n=0
while IFS= read -r pat || [[ -n "$pat" ]]; do
  n=$((n + 1))
  pat="${pat%%#*}"
  pat="$(sed -e 's/[[:space:]]*$//' <<<"$pat")"
  [[ -z "$pat" ]] && continue
  if ((${#files[@]})); then
    # contents (text files only)
    hits="$(grep -H -I -n -i -E -e "$pat" -- "${files[@]}" 2>/dev/null | cut -d: -f1,2 || true)"
    if [[ -n "$hits" ]]; then
      while IFS= read -r h; do echo "LEAK  $h  (denylist line $n)"; done <<<"$hits"
      fail=1
    fi
    # file names
    if printf '%s\n' "${files[@]}" | grep -q -i -E -e "$pat"; then
      echo "LEAK  a file path matches denylist line $n"
      fail=1
    fi
  fi
  if [[ "$mode" == "all" ]] && git rev-parse -q --verify HEAD >/dev/null; then
    # Commit messages only. Author/committer identity is intentionally public and
    # is enforced separately through PGVID_ALLOWED_EMAIL.
    if git log --all --format='%B' | grep -q -i -E -e "$pat"; then
      echo "LEAK  a commit message matches denylist line $n"
      fail=1
    fi
  fi
done <"$list"

if [[ "$mode" == "all" ]] && git rev-parse -q --verify HEAD >/dev/null; then
  while IFS= read -r e; do check_email "$e" "commit"; done < <(git log --all --format='%ae%n%ce' | sort -u)
fi

if ((fail)); then
  echo "check_leaks: FAILED" >&2
else
  echo "check_leaks: OK (${#files[@]} files)"
fi
exit $fail
