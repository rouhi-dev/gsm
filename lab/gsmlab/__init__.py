"""gsm-lab: declarative manager for a multi-node, multi-PLMN Osmocom GSM laboratory.

Layers (each in its own module, each testable without hardware):

    model.py     load + normalise the YAML lab description (nodes, plmns, instances, networks, deployments)
    services.py  what each Osmocom service is: binaries, default ports, which ports are fixed
    validate.py  static checks: references, PLMN/point-code consistency, port/path collisions, capabilities
    render.py    lab model -> one Osmocom .cfg per instance (the topology is authoritative)
    nodes.py     run shell on a node (local or over SSH), probe it (addresses, ports, binaries)
    runtime.py   start/stop/status of instances: pidfiles + cmdline check, dependency order, tmux backend
    health.py    health levels: process -> VTY -> service links -> end-to-end
    repo.py      phase 0: clone/update/auth checks of the gsm superproject (wraps ./gsm)
    cli.py       the gsm-lab command
"""

__version__ = "0.1.0"
