#!/usr/bin/env bash
# Create and verify the GitHub forks that .gitmodules points to.
#
#   tools/create-forks.sh [--dry-run] [--push-missing-tags]
#
# Run it from anywhere; it finds the gsm repository from its own location. It needs the GitHub CLI
# (`gh`), logged in as the account that owns the fork URLs in .gitmodules.
#
# For each submodule, in components.conf (dependency) order:
#   1. read the fork URL from .gitmodules and the pinned commit from the gsm index;
#   2. if github.com/<owner>/<name> does not exist, fork osmocom/<name> (the parent named in
#      components.conf's mirror) -- every command's output and exit code is printed and logged;
#      an existing repo that is NOT a fork of osmocom/<name> is reported and never touched;
#   3. wait until the fork is reachable over git;
#   4. prove the pinned commit is reachable from a ref *in the fork*: its release tag must exist in the
#      fork and point at exactly that commit. (Commit lookups through the API are not proof: GitHub
#      resolves objects across the whole fork network.) --push-missing-tags pushes the tag from the
#      local submodule if it is missing;
#   5. libosmo-sigtran only: once the fork is reachable and has a master branch, set its default
#      branch to master (the osmocom mirror's default is a developer branch), then re-read it.
#
# Never deletes, renames, force-pushes or overwrites anything. Exits non-zero if any step failed.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0; PUSH_TAGS=0
while (( $# )); do case "$1" in
	--dry-run) DRY=1; shift;; --push-missing-tags) PUSH_TAGS=1; shift;;
	-h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
	*) echo "unknown option: $1 (try --help)" >&2; exit 2;; esac; done

WAIT_SECS="${FORK_WAIT_SECS:-300}"       # forks are created asynchronously; large repos take a while
LOGDIR="$ROOT/_build/logs"; mkdir -p "$LOGDIR"
LOG="$LOGDIR/create-forks-$(date +%Y%m%d-%H%M%S).log"
export GIT_TERMINAL_PROMPT=0
FAILS=0
ok()   { printf '[ OK ] %s\n' "$*" | tee -a "$LOG"; }
fail() { printf '[FAIL] %s\n' "$*" | tee -a "$LOG"; FAILS=$((FAILS+1)); }
info() { printf '       %s\n' "$*" | tee -a "$LOG"; }
# attempt <description> <command...>: runs it, logs the full output, prints it on failure
attempt() {
	local desc="$1"; shift; local out rc
	printf '### %s\n$ %s\n' "$desc" "$*" >>"$LOG"
	out="$("$@" 2>&1)"; rc=$?
	printf '%s\n(exit %d)\n' "$out" "$rc" >>"$LOG"
	if (( rc )); then
		printf '[FAIL] %s (exit %d): %s\n' "$desc" "$rc" "$*" | tee -a "$LOG"
		printf '%s\n' "${out:-(no output)}" | tail -n 8 | sed 's/^/       | /'
		FAILS=$((FAILS+1))
	fi
	ATTEMPT_OUT="$out"; return "$rc"
}

echo "gsm repository: $ROOT"
echo "log file:       $LOG"
[[ -f "$ROOT/.gitmodules" && -f "$ROOT/components.conf" ]] || { echo "[FAIL] $ROOT is not the gsm repository"; exit 2; }

# ---- preflight: gh present, logged in, as the owner the .gitmodules URLs expect
command -v gh >/dev/null || { echo "[FAIL] GitHub CLI 'gh' not found (https://cli.github.com)"; exit 2; }
attempt "gh auth status" gh auth status || { echo "       -> gh auth login"; exit 2; }
attempt "determine logged-in account" gh api user --jq .login || exit 2
LOGIN="$ATTEMPT_OUT"
OWNERS="$(git -C "$ROOT" config -f .gitmodules --get-regexp '^submodule\..*\.url$' |
	sed -E 's#.* https://github\.com/([^/]+)/.*#\1#' | sort -u)"
if [[ "$(wc -l <<<"$OWNERS")" != 1 ]]; then echo "[FAIL] .gitmodules points to several owners: $OWNERS"; exit 2; fi
if [[ "${OWNERS,,}" != "${LOGIN,,}" ]]; then
	echo "[FAIL] gh is logged in as '$LOGIN', but .gitmodules expects forks under '$OWNERS'"
	echo "       -> gh auth login (as $OWNERS), or: gh auth switch --user $OWNERS"
	exit 2
fi
ok "gh logged in as $LOGIN (matches .gitmodules)"
gh auth status 2>&1 | grep -i 'scopes' | sed 's/^ */       token /' | sed 's/gh[pousr]_[A-Za-z0-9_]*/[redacted]/g'

declare -a ROWS
while read -r n p tag _up _; do
	[[ -z "$n" || "$n" == \#* ]] && continue
	echo; echo "== $n" | tee -a "$LOG"
	url="$(git -C "$ROOT" config -f .gitmodules "submodule.$n.url")"
	full="$(sed -E 's#^https://github\.com/##; s#\.git$##' <<<"$url")"
	pin="$(git -C "$ROOT" ls-files -s -- "$p" | awk '$1=="160000"{print $2}')"
	parent="osmocom/$n"
	status="?" commit="not verified" defbr="-"
	if [[ -z "$pin" ]]; then fail "$n: no pinned commit recorded at $p"; ROWS+=("$n|error|-|-"); continue; fi
	info "fork $full  <-  parent $parent;  pinned ${pin:0:12} ($tag)"

	# -- exists?
	meta=""
	if meta="$(gh api "repos/$full" --jq '[(.fork|tostring), (.parent.full_name // ""), .default_branch] | join("|")' 2>&1)"; then
		IFS='|' read -r isfork par defbr <<<"$meta"
		if [[ "$isfork" != true || "${par,,}" != "${parent,,}" ]]; then
			fail "$full exists but is not a fork of $parent (fork=$isfork parent=${par:-none}); left untouched"
			ROWS+=("$n|conflict|-|$defbr"); continue
		fi
		ok "$full already exists (fork of $par)"; status="existed"
	elif grep -q 'HTTP 404' <<<"$meta"; then
		if (( DRY )); then info "dry run: would run: gh repo fork $parent --clone=false"; ROWS+=("$n|would create|-|-"); continue; fi
		if attempt "fork $parent" gh repo fork "$parent" --clone=false; then
			ok "fork request accepted for $parent"; status="created"
		else
			info "retrying through the REST API"
			if attempt "fork $parent (REST)" gh api -X POST "repos/$parent/forks"; then
				ok "fork request accepted for $parent (REST)"; status="created"
			else ROWS+=("$n|fork failed|-|-"); continue; fi
		fi
	else
		fail "cannot query $full:"; printf '%s\n' "$meta" | tail -n 4 | sed 's/^/       | /'
		ROWS+=("$n|query failed|-|-"); continue
	fi

	# -- wait until reachable over git (forks are created asynchronously)
	reach=0; t0=$SECONDS
	while (( SECONDS - t0 < WAIT_SECS )); do
		if git ls-remote --exit-code "$url" HEAD >/dev/null 2>&1; then reach=1; break; fi
		sleep 5
	done
	if (( ! reach )); then
		attempt "git ls-remote $url" git ls-remote --exit-code "$url" HEAD
		fail "$full not reachable over git after ${WAIT_SECS}s"; ROWS+=("$n|$status, unreachable|-|-"); continue
	fi
	ok "$full reachable over git ($(( SECONDS - t0 ))s)"

	# -- pinned commit reachable from a ref in the fork (its release tag)
	tagsha="$(git ls-remote "$url" "refs/tags/$tag" "refs/tags/$tag^{}" 2>>"$LOG" | sort -k2 | tail -n1 | cut -f1)"
	if [[ -z "$tagsha" && $PUSH_TAGS == 1 && -e "$ROOT/$p/.git" ]]; then
		attempt "push tag $tag to $full" git -C "$ROOT/$p" push "$url" "refs/tags/$tag" &&
			tagsha="$(git ls-remote "$url" "refs/tags/$tag" "refs/tags/$tag^{}" | sort -k2 | tail -n1 | cut -f1)"
	fi
	if [[ "$tagsha" == "$pin" ]]; then ok "pinned commit ${pin:0:12} present in fork (tag $tag)"; commit="verified"
	elif [[ -z "$tagsha" ]]; then fail "tag $tag missing in $full, so the pinned commit is not proven present"
		info "-> re-run with --push-missing-tags (after ./gsm init), or: git -C $p push $url refs/tags/$tag"; commit="MISSING"
	else fail "tag $tag in $full points to ${tagsha:0:12}, but gsm pins ${pin:0:12}"; commit="MISMATCH"; fi

	# -- current default branch (and the libosmo-sigtran fix)
	defbr="$(gh api "repos/$full" --jq .default_branch 2>>"$LOG")"
	if [[ "$n" == libosmo-sigtran && "$defbr" != master ]]; then
		if (( DRY )); then info "dry run: would set default branch master (currently $defbr)"
		elif git ls-remote --exit-code "$url" refs/heads/master >/dev/null 2>&1; then
			if attempt "set $full default branch to master" gh repo edit "$full" --default-branch master; then
				defbr="$(gh api "repos/$full" --jq .default_branch 2>>"$LOG")"
				[[ "$defbr" == master ]] && ok "$full default branch is now master" ||
					fail "$full default branch still '$defbr' after edit"
			fi
		else fail "$full has no master branch yet; default branch left as '$defbr'"; fi
	fi
	ROWS+=("$n|$status|$commit|$defbr")
done < "$ROOT/components.conf"

echo
printf '%-16s %-22s %-14s %s\n' REPOSITORY STATUS PINNED-COMMIT DEFAULT-BRANCH | tee -a "$LOG"
for r in "${ROWS[@]}"; do IFS='|' read -r a b c d <<<"$r"; printf '%-16s %-22s %-14s %s\n' "$a" "$b" "$c" "$d"; done | tee -a "$LOG"
echo
if (( FAILS )); then echo "Result: $FAILS failure(s). Full command output: $LOG"; exit 1; fi
echo "Result: OK -- all forks exist and contain their pinned commits. Next: ./gsm init"
