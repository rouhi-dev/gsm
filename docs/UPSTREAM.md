# Upstream, mirrors and forks

## Where Osmocom code actually lives

| Role | Location | Notes |
|---|---|---|
| **Official repositories** | `https://gitea.osmocom.org/<group>/<name>` | Canonical source of truth. Libraries are under `osmocom/`, network elements under `cellular-infrastructure/`. |
| **Code review / contributions** | `https://gerrit.osmocom.org` | All patches go through Gerrit. libosmo-sigtran's README still names the Gerrit project `libosmo-sccp` (the repo's former name). |
| **GitHub mirrors** | `https://github.com/osmocom/<name>` | Mirrors maintained by Osmocom. They're not where development happens, and GitHub pull requests aren't Osmocom's contribution path. |

Evidence for the URLs in `components.conf`: each component's own README at the pinned tag names its
official gitea URL and Gerrit queue. For example osmo-msc 1.16.0's README says *"You can clone from the
official osmo-msc.git repository using `git clone https://gitea.osmocom.org/cellular-infrastructure/osmo-msc`"*.

| Component | Official (gitea.osmocom.org) | GitHub mirror |
|---|---|---|
| libosmocore | osmocom/libosmocore | osmocom/libosmocore |
| libosmo-netif | osmocom/libosmo-netif | osmocom/libosmo-netif |
| libosmo-abis | osmocom/libosmo-abis | osmocom/libosmo-abis |
| libosmo-sigtran | osmocom/libosmo-sigtran | osmocom/libosmo-sigtran |
| osmo-mgw | cellular-infrastructure/osmo-mgw | osmocom/osmo-mgw |
| osmo-hlr | cellular-infrastructure/osmo-hlr | osmocom/osmo-hlr |
| osmo-msc | cellular-infrastructure/osmo-msc | osmocom/osmo-msc |
| osmo-bsc | cellular-infrastructure/osmo-bsc | osmocom/osmo-bsc |
| osmo-bts | cellular-infrastructure/osmo-bts | osmocom/osmo-bts |
| osmo-trx | cellular-infrastructure/osmo-trx | osmocom/osmo-trx |

Mirror quirks observed (October 2026):

- `osmocom/libosmo-sigtran`'s **default branch is `daniel/tcap-routing`**, not `master`. A fresh
  fork inherits that, so set the fork's default branch to `master` (`tools/create-forks.sh` does this for
  forks it creates).
- The `osmocom/libosmo-sigtran` mirror carries only 3 tags (2.2.0, 2.2.1, 2.3.0), even though the full
  commit history back to 2009 is there. Release tags up to 1.9.0 are on the old-name mirror
  `osmocom/libosmo-sccp`. 2.0.x/2.1.x tags were in neither GitHub mirror when checked, and
  `libosmo-sccp-legacy` has no GitHub mirror. Fetch from `upstream` (gitea) if you need those tags.
- None of the ten repos use Git LFS. Only osmo-trx has a nested submodule (`osmocom-bb`, pointing at
  gitea.osmocom.org/phone-side/osmocom-bb), needed only for `--with-mstrx`.

## Why native forks of the GitHub mirrors

GitHub can only fork repositories hosted on GitHub, so the forks come from the Osmocom-run mirrors.

- **Native fork (chosen):** full history, branches and tags as of fork time; GitHub shows "forked
  from osmocom/…"; sharing storage with the parent costs nothing; one click (or `gh repo sync`) updates
  the default branch. Downsides: forks of public repos are always public, and only one fork per repo per
  account.
- *Mirror-push into new, unrelated repos:* gives you every ref and allows private repos, but no visible
  upstream link and you sync manually. Worth it only if you need privacy.
- *Submodules pointing straight at upstream:* the simplest option, but you'd have nowhere to keep your own
  patches. It's still available at any time with `./gsm init --source mirror|gitea`.

Inside every submodule, `./gsm init` sets up:

```
origin    https://github.com/rouhi-dev/<name>.git      your fork: where your branches go
upstream  https://gitea.osmocom.org/<group>/<name>      official
mirror    https://github.com/osmocom/<name>             GitHub mirror (fallback for fetches)
```

## Keeping forks in sync

GitHub's **Sync fork** / `gh repo sync rouhi-dev/<name>` fast-forwards **only the default branch**. It
doesn't copy new tags or other branches. Because the gsm pins are release tags, push the tags you need:

```bash
cd libs/libosmocore
git fetch upstream --tags            # or: git fetch mirror --tags
git push origin refs/tags/1.15.0     # only the tag(s) you are about to pin
git push origin upstream/master:refs/heads/master   # optional: same as "Sync fork"
```

Never force-push to `master` in a fork. Keep your own work on separate branches (e.g. `rouhi/feature`)
so syncing stays a fast-forward.

## Moving a pin (bump)

```bash
./gsm bump osmo-msc 1.17.0           # fetches from upstream (falls back to mirror), checks the fork has it
./gsm bump osmo-msc 1.17.0 --push    # ...and pushes the tag to your fork if it's missing
./gsm verify                         # re-checks configure.ac minimum versions against the other pins
./gsm build osmo-msc                 # rebuild (rebuild dependents too if you bumped a library)
git commit -m "bump osmo-msc to 1.17.0"
```

`bump` refuses to pin a commit your fork doesn't have. A gsm commit that points at a missing commit
would break `git clone --recurse-submodules` for everyone else.

Osmocom releases all components together (e.g. the 2026-08-20 batch pinned here). Bump library
releases together with the applications released alongside them. `./gsm verify` catches minimum-version
violations, but not every API/ABI incompatibility. Building is the real test.

## Contributing upstream

Patches for Osmocom go to Gerrit, not GitHub:
<https://osmocom.org/projects/cellular-infrastructure/wiki/Gerrit>. Your fork is the place to develop,
share and pin your own changes. To use a patched component in gsm, push your branch to the fork, then
`./gsm bump <name> <commit>`. `bump` accepts commit SHAs that are on a branch in your fork.
