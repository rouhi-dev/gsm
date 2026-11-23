# open-scenario: A-worker lab topology

A self-contained scenario for testing the osmo-bsc A-worker architecture. `topology.yaml` is the only source of truth:
`scenario.py generate` renders every element's configuration and one tmuxinator project per node into `generated/`.
Never edit `generated/`; change `topology.yaml` and regenerate (`scenario.py check` reports stale files).

```
./scenario.py generate          # topology.yaml -> generated/
./scenario.py check             # topology, binaries, generated files, tmuxinator projects
./scenario.py check --native    # + every program parses its own generated config (needs free ports)

tmuxinator start -p generated/tmuxinator/core.yml   # then bsc.yml, then ran.yml (each waits for what it needs)
./scenario.py status            # STP ASPs, MSC peers, BSSMAP, workers, Supervisor session, BTS, phone
./scenario.py stop ran; ./scenario.py stop bsc; ./scenario.py stop core
```

`tmuxinator stop` works too once a project is visible to tmuxinator (link `generated/tmuxinator/<node>.yml` to
`~/.config/tmuxinator/open-scenario-<node>.yml`): its `on_project_stop` runs `scenario.py stop <node>`. A bare
`tmux kill-session` is not enough: osmo-bsc, osmo-bts and virtphy ignore SIGHUP. Attach with
`tmux -L open-scenario attach -t open-scenario-<node>`. Logs: `<run_dir>/<process>/out.log`.

**BSC execution mode:** `elements.bsc-01.mode` in `topology.yaml`, then `./scenario.py generate`:

| mode | processes on node `bsc` | `bsc-01.cfg` |
|---|---|---|
| `legacy` | `osmo-bsc` (forks its own workers) | `a-worker-pool` without `mode`/`supervisor-socket` |
| `supervisor` | `bsc-aworker` (runs the workers), then `osmo-bsc` (attaches over the session socket) | `mode supervisor` + `supervisor-socket`; the same file is read by both programs |

**Topology:** node `core` (osmo-stp, osmo-hlr, osmo-mgw, two osmo-msc in an NRI pool), node `bsc` (osmo-bsc,
bsc-aworker, osmo-mgw), node `ran` (osmo-bts-virtual, osmocom-bb virtphy + mobile over virtual Um). One A link per MSC:
one worker, one cs7 instance, one BSC point code each; M3UA over SCTP to the STP. On one machine every node is one
loopback address (127.0.41.x); for separate machines set `nodes.<id>` (host, address, run_dir) and use the same
directory path there.

**Notes**
* The ports of osmo-bsc's Abis (3002/3003), osmo-hlr's GSUP (4222), M3UA (2905) and SGs (29118) collide with any other
  Osmocom lab on this machine that binds them on 0.0.0.0: run one of them at a time, or this one in a network
  namespace.
* osmo-bts ends itself when its BSC goes away; re-run its window after restarting osmo-bsc.
* Virtual Um is IP multicast (239.193.41.x): BTS and phone must be on one host or a multicast-capable LAN.
* Two A links to the same MSC are not supported by this osmo-bsc (plan item 2c); `check` rejects them.
