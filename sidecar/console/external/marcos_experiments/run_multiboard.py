"""Run marcos_experiments/mutliboard.py against Adelpha's copy of marcos_client."""
import os
import sys
from importlib import import_module

HERE = os.path.dirname(os.path.abspath(__file__))
CONSOLE = os.path.abspath(os.path.join(HERE, "..", ".."))  # sidecar/console
sys.path.insert(0, CONSOLE)
sys.path.insert(0, HERE)

for name in ("local_config", "marmachine", "grad_board", "server_comms", "marcompile",
             "experiment", "experimentController", "experimentFollower",
             "experimentExternal", "multiExp"):
    sys.modules[name] = import_module(f"external.marcos_client.{name}")

import mutliboard

if __name__ == "__main__":
    mutliboard.multi_experiment()