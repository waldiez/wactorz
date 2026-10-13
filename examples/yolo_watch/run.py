# SPDX-License-Identifier: AGPL-3.0-only
# This example uses Ultralytics (AGPL-3.0); see its README.
"""Start Wactorz with the snapshot detector, and the camera watcher when a camera is named.

python run.py              # snapshots on camera/+/snapshot only
CAMERA=0 python run.py     # plus the first camera, read directly
CAMERA=rtsp://... python run.py
"""

import os

from agent import CameraWatcher, detect_in_snapshot

import wactorz

if __name__ == "__main__":
    agents = [detect_in_snapshot]
    if os.environ.get("CAMERA"):
        agents.append(CameraWatcher)
    wactorz.run(agents=agents, minimal=True, state_dir="./state")
