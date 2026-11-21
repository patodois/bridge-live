# Agent guide

bridge-live is the live interop matrix of rewrite-bridge's passthrough link: it starts real game builds (Minecraft dev
clients of the minecraft-link mods, the headless Hytale server with its plugin) against scripted peers and each other,
judges each session from both sides' counters and logs, and compares a run with the previous one. It runs on macOS.

## Toolchain, build and CI

- Python 3.9+ standard library only (`./live` uses `/usr/bin/python3`). No package to install.
- `./live` builds the scripted peer `peer/` with `cargo build --release --locked` (Rust 2021) and the screen helper
  `tools/screenstate.swift` with `swiftc`. `live_peer` depends on rewrite-bridge pinned by git revision (contract
  revision 8, see README "Contract revision"); never build it without `--locked`.
- The matrix itself needs the sibling checkouts named in `config.json` (or `config.local.json`) and opens game windows:
  it is not run in CI and must not be run by an agent unless the maintainer asks for it.
- Exact CI verification commands:

```sh
python3 -m unittest discover -s tests
python3 -m unittest discover -s tools -p 'test_*.py'
python3 tools/check_public_tree.py
```

The unit tests use fakes only (no game, no peer, no build); `tests/test_jfr_summary.py` is skipped unless the JDK of
`config.json` is installed. `tests/support.py` points `gameenv.real_home()` at an empty temporary folder for every test.

## Code map

- `live`, `orchestrator/live.py`: the command line, one run, the exit status, the report writer.
- `orchestrator/matrix.py`: the pairs, their scenarios and checks. `mc.py`: Minecraft clients, AutoTest scripts,
  `options.txt`. `peers.py`: the scripted peer, bridge-guest, LinkDemo, the Bevy host, the Hytale server.
- `orchestrator/harness.py`: one pair's context; `Ctx.start_game` is the only way a game is started.
- `orchestrator/gameenv.py`: scratch home, profile guard, write-deny sandbox, asset cache, profile manifest.
- `orchestrator/report.py`: summaries and the comparison with earlier runs. `jfr.py`, `tools/JfrSummary.java`: profiling.
- `peer/`: `live_peer`, the scripted Rust peer. `tests/`: unit tests. `tools/check_public_tree.py`: the public-tree gate.

## Project rules

- Every game process goes through `Ctx.start_game` (scratch HOME, launch guard, `sandbox-exec` write deny); no game
  starts without a working sandbox. `tests/test_game_home.py` fails on any other process start.
- No launcher folder (Minecraft, Prism, Hytale user data, ...) is read by default. The asset cache is seeded only from
  a folder the caller names (`--minecraft-assets`, `BRIDGE_LIVE_MINECRAFT_ASSETS`); the profile manifest runs only with
  `--check-profiles`. Keep the README "Safety" table exact when this changes.
- Never `rm`, `pkill` or `killall`: deletion goes through `common.safe_delete`, processes are ended by PID.
- Everything binds `127.0.0.1`. Nothing signs in, and no launcher or retail client is started.
- A pair that ran keeps its judged status; only a pair not started because of the screen is `skipped`.
- Reports (`reports/`, `logs/`) stay local and are written through `common.scrub` (paths relative to the repository,
  home folders as `~`).

## Public files

- Never commit game files, extracted assets, screenshots, run reports, logs, decompiled code or secrets.
- No tracked file may name a real home folder or a person; `tests/test_public_files.py` checks the home folders.
- The public-tree gate needs Python 3.9+ and Git: `python3 tools/check_public_tree.py` (checks Git's index); CI runs
  it with `--range BASE..HEAD`.

## Contributing, license and AI

- Open an issue to discuss a problem or send a pull request with the change.
- Before a pull request, run the CI commands above.
- License: MIT OR Apache-2.0, at your option; see `LICENSE-MIT` and `LICENSE-APACHE`.

The code was written with AI coding agents (Anthropic's Claude models) under the maintainer's direction.
AI-assisted contributions are welcome when their use is disclosed and the changes are reviewed.
