#!/usr/bin/env bash
# Create (or check) native GitHub forks of the Osmocom mirrors listed in components.conf.
#
#   tools/create-forks.sh [--owner LOGIN] [--dry-run]
#
# Requires an authenticated GitHub CLI (`gh auth login`) for the account that should own the forks.
#
# Safe by design:
#   * never deletes, renames, force-pushes or overwrites anything;
#   * an existing repo with the same name that is NOT a fork of osmocom/<name> is reported and left alone;
#   * for forks it creates, sets the default branch to `master` (the osmocom/libosmo-sigtran mirror's
#     default branch is a developer branch); existing forks are only checked, not changed;
#   * finally checks that every pinned release tag is present in the fork with the expected commit.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER=""; DRY=0
while (( $# )); do case "$1" in
	--owner) OWNER="$2"; shift 2;; --dry-run) DRY=1; shift;;
	*) echo "usage: $0 [--owner LOGIN] [--dry-run]" >&2; exit 2;; esac; done

command -v gh >/dev/null || { echo "[FAIL] GitHub CLI 'gh' not found (https://cli.github.com)"; exit 2; }
gh auth status >/dev/null 2>&1 || { echo "[FAIL] gh is not authenticated: run 'gh auth login'"; exit 2; }
[[ -n "$OWNER" ]] || OWNER="$(gh api user --jq .login)" || { echo "[FAIL] cannot determine GitHub login"; exit 2; }
echo "Fork owner: $OWNER$( ((DRY)) && echo '  (dry run: nothing will be created)')"

created=(); existed=(); failed=()
while read -r n p tag up _; do
	[[ -z "$n" || "$n" == \#* ]] && continue
	pin="$(git -C "$ROOT" ls-files -s -- "$p" | awk '{print $2}')"
	info="$(gh api "repos/$OWNER/$n" --jq '[(.fork|tostring), (.parent.full_name // ""), .default_branch] | join("|")' 2>/dev/null)"
	if [[ -n "$info" ]]; then
		IFS='|' read -r isfork parent defbr <<<"$info"
		if [[ "$isfork" != true || "${parent,,}" != "osmocom/${n,,}" ]]; then
			echo "[FAIL] $OWNER/$n exists but is not a fork of osmocom/$n (fork=$isfork parent=${parent:-none}); left untouched"
			failed+=("$n"); continue
		fi
		echo "[ OK ] $OWNER/$n already exists (fork of $parent, default branch $defbr)"
		[[ "$defbr" != master ]] && echo "[WARN] $OWNER/$n default branch is '$defbr'; consider: gh repo edit $OWNER/$n --default-branch master"
		existed+=("$n")
	else
		if (( DRY )); then echo "[ -- ] would fork osmocom/$n -> $OWNER/$n"; continue; fi
		if ! gh repo fork "osmocom/$n" --clone=false --remote=false >/dev/null 2>&1 &&
		   ! gh api -X POST "repos/osmocom/$n/forks" >/dev/null; then
			echo "[FAIL] forking osmocom/$n failed"; failed+=("$n"); continue
		fi
		for _ in $(seq 1 30); do gh api "repos/$OWNER/$n" >/dev/null 2>&1 && break; sleep 4; done   # forks are created async
		defbr="$(gh api "repos/$OWNER/$n" --jq .default_branch 2>/dev/null)"
		if [[ -z "$defbr" ]]; then echo "[FAIL] $OWNER/$n did not appear after forking"; failed+=("$n"); continue; fi
		[[ "$defbr" != master ]] && gh repo edit "$OWNER/$n" --default-branch master >/dev/null && defbr=master
		echo "[ OK ] created $OWNER/$n (fork of osmocom/$n, default branch $defbr)"
		created+=("$n")
	fi
	# the pinned release must be fetchable from the fork, or `./gsm init` will fail for everyone
	got=""
	for _ in $(seq 1 15); do
		got="$(git ls-remote "https://github.com/$OWNER/$n.git" "refs/tags/$tag^{}" "refs/tags/$tag" 2>/dev/null | sort -k2 | tail -1 | cut -f1)"
		[[ -n "$got" ]] && break; sleep 4; done
	if [[ "$got" == "$pin" ]]; then echo "       tag $tag -> ${pin:0:12} present in fork"
	else echo "[FAIL] $OWNER/$n: tag $tag is ${got:-missing}, gsm pins ${pin:0:12}"
	     echo "       -> git -C $p push https://github.com/$OWNER/$n.git refs/tags/$tag"; failed+=("$n"); fi
done < "$ROOT/components.conf"

echo
echo "created: ${created[*]:-none}"
echo "already existed: ${existed[*]:-none}"
echo "failed: ${failed[*]:-none}"
(( ${#failed[@]} == 0 ))
