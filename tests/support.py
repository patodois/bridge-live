"""Shared test isolation: no test may look at the account's real launcher folders.

`gameenv.real_home()` names the account's home folder (from the password database); the profile guard, the sandbox
deny list and the profile manifest are built from it, and `os.path.realpath` on those paths looks at the real folders'
entries. Every test module calls `start()` in `setUpModule` and `stop()` in `tearDownModule`: while a module runs,
`real_home()` is an empty temporary folder and BRIDGE_LIVE_MINECRAFT_ASSETS is unset, so nothing a test does can reach
a player's Minecraft, Prism or Hytale folder.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

import gameenv  # noqa: E402

_state = []


def start():
    home = Path(tempfile.mkdtemp(prefix="bl-fake-home-")).resolve()
    patches = [mock.patch.object(gameenv, "real_home", return_value=str(home)),
               mock.patch.dict(os.environ, {}, clear=False)]
    for p in patches:
        p.start()
    os.environ.pop(gameenv.ASSETS_SEED_ENV, None)
    _state.append((home, patches))
    return home


def stop():
    home, patches = _state.pop()
    for p in reversed(patches):
        p.stop()
    shutil.rmtree(home, ignore_errors=True)


def fake_home():
    return _state[-1][0]
