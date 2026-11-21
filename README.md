# bridge-live

Test layer 4 for the [rewrite-bridge](https://github.com/patodois/rewrite-bridge) passthrough link: an automated **live interop
matrix with the real games** on macOS. One command starts each pair of programs that should be able to talk
(a Minecraft dev client and a scripted peer, a Minecraft client and the Hytale server, two Minecraft clients, `bridge-guest` and a Bevy host, ...),
drives a scripted scenario, judges it from the protocol counters and log lines each side prints, screenshots the game window,
shuts everything down and compares the run with the previous one.

| Layer | Repository | What it covers |
|---|---|---|
| 1 | each library's own tests | wire format, shared vectors, loopback sessions |
| 2 | `bridge-verify` | the asset readers against the installed games |
| 3 | `rewrite-bridge/tests/interop` | every host/guest pair of link-rust, -java, -c, -cs, scripted peers only |
| **4** | **this repository** | **the real games and mods as the two ends of a session** |

It makes no sound, never writes into a player's real Minecraft, Prism or Hytale profile or saves and does not read them unless asked to,
writes into the mod checkouts only the few files listed under "Safety", and never runs `rm`. It is a harness for the author's own machine layout:
the sibling checkouts, JDKs and the installed Hytale package are named in `config.json` (override them in `config.local.json`).

## Run it

```sh
./live                         # every pair, one at a time (about 40 minutes); exit 0 only if every pair ran and passed and there is nothing to alert
./live --token                 # the token matrix (contract section 6.2, 26 pairs) instead of the plain one; a partial run, never a baseline
./live --only mc-26.3-fabric   # the pairs whose id contains one of the comma-separated texts (a partial run, never a baseline)
./live --skip-gui              # only the pairs that open no window (the Hytale ones), for a locked screen
./live --minecraft-assets DIR  # seed the vanilla asset cache from DIR once (see "Safety"); without it the cache starts empty
./live --check-profiles        # list the real launcher folders before and after the run and alert on a change (off by default)
./live --list                  # the pairs, and which cannot run here
./live doctor                  # screen state, RAM, disk, what is available
./live summary [report.json]   # print a summary again
./live compare OLD NEW         # the alerts NEW raises against OLD
./live clean                   # delete work/probe, work/hytale-server (a regenerated scratch world), work/target-bevy, work/scratch, work/cache
```

Exit status: 0 every pair ran and passed and there is nothing to alert; 1 a pair failed (`fail`, `error`, `aborted`) or there are alerts; 2 the tool
itself failed; 3 nothing failed, but not every pair ran (`skipped` on a locked screen, or `not_available`: a missing checkout, tool or JDK) or no pair passed.

**The game windows appear on the screen, and the screen must be unlocked.** A pair that opens a window is not started when the screen is locked
or the display asleep (`screenstate` helper, CoreGraphics session dictionary): it is recorded as `skipped` with the reason, never as a failure.
A pair that was started stays what it was judged to be: a lock that arrives *during* a pair does not turn a failure into `skipped`; it is written in the pair's notes. A run with a skipped pair is reported but is **not a baseline** (it is written to
`logs/<stamp>/report.json`, not `reports/`). The runner never types into anything, takes no focus on its own (a game window takes the focus once when it opens, as any
app does) and never clicks. While a run is going it holds a `caffeinate -d -i` power assertion tied to its pid, because an idle display sleeps after ten minutes
and locks the screen halfway through the matrix (that happened in the first attempt); it changes no setting and ends with the run.

## The pairs (50)

`<minecraft version>-<loader>/<scenario>`; one Minecraft client is started per pair, at most one pair runs at a time.

| Scenario | Minecraft client is | The other side | What is checked |
|---|---|---|---|
| `peer-guest` | host | `live_peer guest --frames` (scripted Rust, `rewrite-bridge` at contract revision 8, see "Contract revision"): spawns a cube 4 m ahead on the host's ground, poses and **frames with depth** for every camera, asks for the input focus, despawns, says bye | both sides' counters, frames received and drawn with reprojection, the pixel file's depth plane, focus handover, bye |
| `peer-host` | guest | `live_peer host --guest-leaves`: cameras, collision snapshots, answers the focus request; MC walks only after the grant | player stood still while the host held the focus, walked after it, spawn/despawn/bye seen by the host |
| `bridge-guest` | host | [`bridge-guest`](https://github.com/patodois/bridge-guest) rendering a Hytale Trork (Bevy, GPU, exact host camera) | frames with depth drawn in the world, summary line of bridge-guest |
| `java-frames` | host | link-java's `LinkDemo guest --frames` (the Java frame sender, never tested against Minecraft before) | as `peer-guest` |
| `hytale-guest` | host | the headless Hytale server with the HytaleLink plugin as **guest** (no player: fallback pose) | Hytale's `LINK CONNECTED`/status/`LINK LOST` lines and the Minecraft side |
| `hytale-host` (one build per version) | guest | the Hytale server with the plugin as **host** | as above; the Minecraft player's pose drives the followed entity |

Builds: Minecraft 26.3 on Fabric, NeoForge and Forge; 1.20.1 on Fabric, Forge and NeoForge; 1.12.2 Forge; 1.7.10 Forge. Plus:

| Pair | |
|---|---|
| `mc-mc/26.3-fabric-host+1.20.1-fabric-guest` and the reverse | two Minecraft clients, both windows on screen, both screenshotted |
| `hytale-host/peer-guest`, `hytale-guest/peer-host` | the Hytale server alone with the scripted peer, in both roles (no window) |
| `hytale-host/bridge-guest` | the Hytale server as host, `bridge-guest` rendering a prop for its fallback camera (no window) |
| `bevy-host/bridge-guest` | `bridge-guest` into `guest_compose` of `rewrite-bridge-bevy` (installed by hand at the pinned revision, see "Contract revision"), screenshot of the Bevy window |

Not in the matrix: Minecraft 1.2.5 (its own repository), Quilt, and `bridge-guest` with the other three providers (only Hytale's Trork is rendered).

## Contract revision

The scripted peer `live_peer` is built from [rewrite-bridge](https://github.com/patodois/rewrite-bridge) at commit `d03035b11e04de5ab9b289bd8c60fb2d25e4f126`,
the commit that implements **contract revision 8** (crate version 0.4.0 in that tree). No crates.io release carries revision 8 yet, so `peer/Cargo.toml` pins the
dependency by its git revision instead of an exact registry version, and `peer/Cargo.lock` records the same commit; once a release with revision 8 is published the pin
goes back to `version = "=x.y.z"`. `./live` builds the peer with `cargo build --release --locked` only (the first build fetches the pinned commit and the locked crates).
The report takes the version and source of `rewrite-bridge` from `peer/Cargo.lock`, and `live_peer`'s own result line carries the version it was built with.

`guest_compose` (the Bevy host of `bevy-host/bridge-guest`) is not built by `./live`. Install it from the same commit:

```sh
cargo install --locked --git https://github.com/patodois/rewrite-bridge --rev d03035b11e04de5ab9b289bd8c60fb2d25e4f126 \
  rewrite-bridge-bevy --example guest_compose --features compose,viewer --root work/tools
```

What the matrix exercises is the token and endpoint file (section 6.2, added in revision 6) and the plain session; the mods, link-java, `bridge-guest` and the Hytale plugin
are whatever revision their checkouts are at (the report records each repository's revision). The rules revision 8 added (candidates and `busy` only during a session,
bounds per poll, pose time order and `TELEPORT`, cleaned peer text) have no dedicated pair yet. The revision 8 commits of the other implementations
when this pin was set: link-java `16ab49e` (used here for `LinkDemo` and, through the mods' composite build, by the Minecraft clients), link-c `f81b05f` and
link-cs `8843da8` (not used by this matrix).

## The token matrix (`./live --token`, contract section 6.2)

The link token (CONTRACT 6.2, added in revision 6) is off by default in the mods, so the 50 pairs above run without it, exactly as before. `./live --token` runs 26 more pairs, all through the same launch
path (`Ctx.start_game`, scratch home, write-deny sandbox, sound off), with `token_auth` switched on in the Minecraft client's `config/minecraft-link.json` and `endpoint_file` pointing at a file in a
private (mode 0700) scratch folder that goes away with the pair. A real token is never written by this tool: the host (the mod through link-java, or `live_peer`) makes it and writes the
endpoint file, the guest reads it. The one token this tool writes is the made-up **wrong** token of the wrong-token pairs, into an endpoint file in the same private folder. Pair ids:

| Pair | What happens | What is checked besides the plain pair's checks |
|---|---|---|
| `mc-<v>-<loader>/token-peer-guest` (8 builds) | Minecraft host with a token, `live_peer guest --endpoint-file` reads its file | the file exists after `link host`, mode 600, the port the host bound, 64 hex digits; gone after the host's clean stop; `/link status` says `token auth on`; nobody refused |
| `mc-<v>-<loader>/token-peer-host` (8 builds) | `live_peer host --endpoint-file` writes a file, the Minecraft guest reads it | the same, for the scripted host's file; the guest connects, walks when granted |
| `mc-<v>-<loader>/token-wrong-guest` (one build per Minecraft version) | Minecraft host with a token; the scripted guest reads an endpoint file with the right port and a **wrong** token | the guest sees exactly `peer said bye: unauthorized` and never connects; the host never connects, counts the refusal in `/link status` (`1 refused as unauthorized`) and tells the player once |
| `mc-<v>-<loader>/token-wrong-host` (one build per Minecraft version) | the Minecraft guest's endpoint file names the scripted host's port with a **wrong** token | the host sees `unauthorized` and never connects; the guest counts it and tells the player once |
| `mc-mc/token-...` (both directions) | two Minecraft clients, both configs name the same endpoint file | both connect, token auth on, no refusal |

Every token pair also scans the console output, the game's `logs/`, `crash-reports/` and this tool's logs of the pair for the tokens it saw (right and wrong) and fails if one appears. The programs that have no token
option are not in it: `bridge-guest`, link-java's `LinkDemo` and the Hytale plugin (they still run in the plain matrix, against a mod with the token off).

`live_peer` takes `--endpoint-file PATH` (a host writes it, a guest reads it) and `--expect-refused` (passes only if the session was refused with `unauthorized` and never connected).

**Other checkouts.** `config.local.json` (git-ignored, optional) is laid over `config.json`: objects merge key by key, lists are replaced. A machine that tests the mods from other worktrees than the ones in `config.json` keeps its paths there.
The mods build link-java as a Gradle composite build from `../link-java`; export `ORG_GRADLE_PROJECT_linkJavaDir=<path>` to point every build at another link-java checkout.

## How a pair runs

1. **Guards.** Free RAM must be at or above 20% (three samples two seconds apart) and free disk at or above 30 GB before a pair starts, and the
   same limits are watched every two seconds while it runs; a breach kills the pair's processes and records it as `aborted`.
2. **Sound off, twice.** For every Minecraft client the game directory's `options.txt` gets `0.0` for the master volume **and every sound category of that version**
   (`soundCategory_master, music, record, weather, block, hostile, neutral, player, ambient, voice` and `ui` on 26.3; `soundVolume` and `musicVolume` on 1.7.10),
   is read back before the launch, and read back again after the game has quit and rewritten the file with what it actually held in memory.
   Both read-backs are checks of the pair. (`pauseOnLostFocus:false` and `fullscreen:false` are written as well, so a click in another app does not pause the test;
   the version's `options` in `config.json` add the accessibility onboarding and tutorial keys the checkout's own `prepareRunOptions` task writes.) On top of that the game is
   started with `ALSOFT_DRIVERS=null`, so OpenAL Soft opens its `null` backend and nothing can reach a speaker whatever `options.txt` says; OpenAL's own log
   (`ALSOFT_LOGFILE`) is read back and a check fails if any other backend was initialised. The checkouts' builds write the same sound-off lines into their `run/options.txt`
   before every `runClient`; that rule of the minecraft-link projects is unchanged.
3. **Launch.** `./gradlew <runClient task> --no-daemon --offline -I <init script> -Pautotest=<script> -Pshots=<dir>` in the version's worktree, in its own session, through
   `Ctx.start_game` (see "Games and real profiles"), with the JDK from `config.json` (JDK 25; JDK 21 for 1.20.1, see "Findings"). The script is generated per pair from the AutoTest commands of `minecraft-link`
   (`create_world`, `cmd`, `link host|guest|status|focus|stop`, `key`, `look`, `pos`, `hud`, `log`, `quit`). The orchestrator reads the game's log as it
   appears and reacts to marker lines the script prints with `log`: `START_PEER <port>` starts the other side, `SHOT` takes the screenshot.
4. **Scenario** as in the table, with a timeout for the whole pair (7 minutes for one client; with two clients 8 minutes for the host, then up to 8 more for the guest). Processes that do not end by themselves are killed **by PID**
   (the tree of the session the pair created), never by name.
5. **Judgement.** Checks that gate the pair (printed in the report with their evidence), plus two kinds of numbers:
   - `exact`: discrete protocol facts (spawns/despawns seen, focus events, `malformed/foreign/stale/unknown_entity` counters, peer names, protocol version, bye seen,
     the pixel file's depth plane, entities shown). Compared exactly with the previous run.
   - `rates`: counts that depend on timing (cameras, poses, frames per session, datagrams, frame latency). Each pair has floors as checks; between runs a drop of more than
     30% is an alert (a live game never delivers the same number of cameras twice). The two-Minecraft pairs are the exception for cameras, see below.
   - **Two Minecraft clients, camera rate.** With both game windows on screen the clients compete for the CPU and the GPU, so the number of cameras the host sends (and the guest receives)
     is not repeatable: runs with identical code gave 906 to 2,249, and a percentage-drop alert against the previous run fired on that noise alone (`mc-mc/1.20.1-fabric-host+26.3-fabric-guest`,
     2,362 then 1,125). For `mc-mc/` pairs `host.cameras_sent`, `guest.cameras_received` and `*.cameras_per_s` are therefore **not** compared between runs. Their judge is a gating
     check in the run itself: cameras sent (host) and received (guest) at **at least 5 per second sustained**, measured over the status lines printed while connected
     (last minus first counter, divided by the wall time between them). The floor is the point where the link is plainly not delivering cameras, far below the slowest
     run seen (about 15 per second), so it fails for a stalled link and not for a busy machine. The percentage alert stays on for every other pair and number.
6. **Screenshot.** `screencapture -l <window id>` of the game window (the window server is asked which windows the pair's processes own), checked not to be blank.
   Screenshots show game content: they stay in `logs/<stamp>/shots/` and are gitignored. Logs of the last three full runs are kept, the others of the newest six partial runs.
7. **Clean-up.** The worlds a run created (`linktest-<epoch>` in the dev run directory; the old versions reuse one fixed world) are deleted by one guarded function; crash reports in the run
   directory are looked for; `ps` is compared with the start of the run, and any process this run started that is still alive is an alert.
   Peak RAM is the sum of the resident set of every process of the pair (Gradle client and its single-use daemon, the game, the peers, the Hytale server) sampled once a second.

Peers (`peer/`, Rust, about 700 lines, built from `rewrite-bridge` at the pinned revision) print one `RBL-INTEROP-RESULT {json}` line with the keys of the interop peers of rewrite-bridge
(`spawns`, `despawns`, `cameras`, `poses`, `focus`, `lost_reason`, `stats`, ...) plus an `extra` object, and exit 1 if any of their own checks failed.

## Results so far (2026-10-25, Apple M5, 16 GB, macOS 26.6.2, JDK 25.0.5)

Two full runs, one after the other (reports `20261025T034943Z` and `20261025T042838Z`, kept locally: reports are not part of the repository), 50 pairs and about 1,050 checks each, 39 minutes each:

| | run 1 | run 2 |
|---|---|---|
| pass / fail | 49 / 1 | 50 / 0 |
| alerts against the previous run | baseline | 1 (the same pair's `lost` reason, below) |
| screenshots taken and not blank | 49 of 49 | 49 of 49 |
| processes left behind | none | none |

Wall time per pair differs by at most 5.4 s between the two runs (one Minecraft client: 34 to 53 s; two clients: 100 s; Hytale alone: 21 s; Bevy: 25 s). Peak RAM of a pair
(Gradle client and daemon, game, peers, Hytale server together): 1.7 to 3.7 GB for one client, 3.8 to 4.2 GB for two; the game JVM alone 1.1 to 2.2 GB. Zero malformed, foreign
(while connected) or unknown-entity datagrams anywhere, `stale` 0 everywhere.

## Findings (real or suspected defects, with evidence; nothing was patched in any repository)

1. **`mc/1.20.1` does not build under JDK 25 here.** `./gradlew help --no-daemon` in `minecraft-link-1.20.1` with Homebrew `openjdk@25` fails in 2 s with
   `BUG! exception in phase 'semantic analysis' in source unit '_BuildScript_' Unsupported class file major version 69` (Gradle 8.14.3). The branch's README ("run Gradle with a JDK 25")
   says it works; it also says its recorded dev-client runs used JDK 21, which is what `config.json` points that version at (`~/.gradle/jdks/eclipse_adoptium-21...`). The other three versions run on JDK 25.
2. **Hytale plugin: a second `spawn` at the start of a session, once.** `hytale-guest/peer-host` saw `guest spawned entity 1` twice, 6 ms apart (the host's `spawns` was `[1, 1]`);
   it passed 22 later runs with one spawn each (in the 18 loop runs made to catch it, the host's control-line counter, `lines_received`, was 2: the hello and one spawn). The code path that explains it: `WorldBridge.tick()` publishes on the
   **world thread** (`world.execute`), while `onConnected()` calls `EntityAnnouncer.forget()` on the **link thread**; if the world thread sees the connected link and announces
   before `forget()` runs, the next publish announces again. Benign for the receivers that replace an entity on a second `spawn`. Not confirmed at the wire level, so no fix was committed;
   the check is non-gating in the matrix. A fix would remember the session id with the announcement.
3. **One session ended with `connection closed` instead of `peer said bye: closing`**: `mc-1.7.10-forge/hytale-host`, run 1. The Minecraft log shows an ordinary `link stop`;
   the plugin saw the connection end without the bye line (link-java reads complete lines before it applies an end-of-stream, so the bye bytes did not arrive). Nine later runs of
   the same pair all saw the bye. Unexplained and not reproducible on demand; the logs of that run are local, not in this repository.
4. **The `foreign` counter can count a late heartbeat after the session has ended** (once in development, MC 26.3 NeoForge host after a bye). It is the known case of `tests/interop/README.md`,
   not a defect; it is why the anomaly counters are judged on the statuses taken while connected, and the value after the bye is only recorded (`mc.foreign_after_bye`).

## Reports

`reports/<UTC>.json` and `.txt` for a full run (alerts first, then one line per pair); a partial run writes `logs/<UTC>/report.json` instead, so it can never become the baseline.
The first full run has nothing to compare with. Against the latest previous full report:

- a pair that failed, or passed before and does not now; a pair that disappeared
- an `exact` value that changed (any difference), or a new one
- a `rate` that fell by more than 30%
- wall time or peak RAM **more than 25% over the maximum of the last three full runs** (fewer: those there are), unless the rise is under 3 s or 32 MB
- a process started by the run that is still alive
- `inputs moved` is printed when a repository's revision, the plugin jar, the server jar or a built binary changed since the previous run, so a change in numbers can be tied to it

Every report records the revision and dirtiness of every repository involved, the sha of the plugin jar and of the scripted peer, the machine, JDK and rustc.
Timings are only comparable between runs made under similar load: each pair records the load average and free RAM when it started. Another job using the GPU
or the CPU (for example `bridge-verify`) at the same time shows up there.

## What cannot be automated

- Seeing that the Hytale entity looks right in a Hytale **client**: the server runs headless (no client, no player), so the plugin publishes its fallback pose and the follow entity
  is checked by the plugin's own status counters, not by eye.
- Whether a Minecraft avatar of the other client is *visible*: with two clients the second world's ground is at a different height (1.20.1 sits at y = -60 with a link offset of +60,
  26.3 at y = 4), so the avatar's position is not asserted; the host's `guest entities` counter and the poses it received are.
- A hand check of every screenshot: the harness checks that a window was captured and is not blank, not that the picture is right. (Read them; `peer-guest`/`bridge-guest` screenshots show the
  prop with a stone wall hiding its right half.)
- Occlusion and reprojection correctness is covered by the mods' own unit tests and the depth tests of `bridge-guest`; here a pair passes when frames with depth were received and
  drawn by the host (`frames drawn`, `reprojected` in `/link status`).

## Games and real profiles

A player's real Minecraft (`~/Library/Application Support/minecraft`, `~/.minecraft`), Prism (`.../PrismLauncher`) and Hytale (`.../Hytale`, with the sign-in data and the
installed game) folders are never written. Every game bridge-live starts (a Minecraft dev client through Gradle, the headless Hytale server) is started by **one function,
`Ctx.start_game`** (`orchestrator/harness.py`), which uses `orchestrator/gameenv.py`:

| What | How |
|---|---|
| Scratch home | `HOME`, the XDG folders and `TMPDIR` of the game are fresh folders under `work/scratch/<run>/<pair>/<client>/`; the game JVM also gets `-Duser.home` and `-Djava.io.tmpdir` there (a JVM on macOS ignores `$HOME`). For Minecraft they are set by a generated Gradle init script (`-I`), in `doFirst` of `runClient`, so only the forked game JVM is touched, not Gradle's own JVM and cache (`GRADLE_USER_HOME` stays the real `~/.gradle`: downloaded libraries, not a profile). |
| Scratch instance | `"instance": "scratch"` on a loader in `config.json` (26.3 Fabric and NeoForge, 1.20.1 Fabric): the game directory is the scratch `instance/` folder (`--gameDir`), so worlds, logs, crash reports and `options.txt` of a run never reach the checkout's `run/` folder. Crash reports are copied to the pair's logs before the scratch folder goes. The Forge builds (26.3 and 1.20.1), NeoForge 1.20.1, 1.12.2 and 1.7.10 pass their own `--gameDir run` (and keep `run/` as the process directory), so they keep the checkout's gitignored dev `run/` folder, as before; their worlds are deleted by `safe_delete` after the run as before. |
| No writes to real profiles | The launch is wrapped in `sandbox-exec` with a profile that denies every file write below the profile folders above (and, for a scratch instance, below the checkout's `run/` folder except its `options.txt`). The mechanism is self-tested against a scratch folder before it is relied on; if it cannot be used the game is not started and the pair is an `error`. |
| No real path in a command | `assert_clear_of_profiles` refuses a launch whose working directory, arguments or environment name a real profile folder. |
| The installed Hytale package (read-only) | The Hytale server needs its Java runtime, `HytaleServer.jar` and `Assets.zip` **from the installed Hytale package** (`hytale.package` in `config.json`). They are only read (the launch declares the path `read_only`, the sandbox denies writes there); the server writes into `work/hytale-server` and the scratch folder. `bridge-guest` also reads Hytale assets from the install, read-only, as a tool. |
| Vanilla assets | Some Forge builds look for the vanilla assets under `$HOME/Library/Application Support/minecraft/assets`; in the scratch home that is a link to bridge-live's own cache `work/cache/minecraft-assets`. The cache starts **empty** (the build downloads what it needs into it). It is seeded from an existing folder only when you name one: `./live --minecraft-assets DIR` or `BRIDGE_LIVE_MINECRAFT_ASSETS=DIR`. `./live clean` deletes it. |
| Proof (opt-in) | `./live --check-profiles` takes a manifest (path, size, mtime) of the profile folders before and after the run; any difference is an alert and the report prints "real launcher profiles ... untouched". Off by default, because it lists the player's folders. |

`tests/test_game_home.py` fails when any code starts a process by another route (a new `subprocess`/`Popen`/`os.system` call outside a short, documented list of helpers, a call to the removed
`Ctx.start`), when the code that builds a game's command line is started as a plain tool, when a module other than `gameenv.py` spells a profile folder, or when `config.json` names one
other than the read-only Hytale package, or when a launcher folder is read without the caller naming it. Run all tests with `python3 -m unittest discover -s tests`
(fakes only; the JFR test needs the JDK of `config.json`). Every test points the account's home folder at an empty temporary folder (`tests/support.py`).

## Profiling with JFR

`./live --jfr` records Java Flight Recorder in the Minecraft clients that can (the game JVM only, not Gradle's) and writes, next to the pair's logs, `logs/<stamp>/<pair>/<client>.jfr-summary.json`
(and the headline numbers into the report):

- CPU seconds per thread (`jdk.ThreadCPULoad` every second, scaled by the machine's hardware threads), allocation per thread (difference of the cumulative counters), GC collections, pause total
  and longest, peak heap at collection, and the busiest methods of our packages (`dev.patodois.minecraftlink`, `dev.patodois.rewritelink`; `jfr.packages` in `config.json`): the mod method
  nearest to the sampled frame, self, and inclusive.
- `tools/JfrSummary.java` needs only a JDK 17+ (`java tools/JfrSummary.java --package a.b recording.jfr`). It streams the recording and reads a handful of event types; it never reads the JVM
  arguments, the program's command line, environment variables or system properties, and a last pass replaces any user path in a string with `~`. The raw `.jfr` (which does contain all of that)
  lives in the scratch folder and is deleted as soon as it has been summarised.
- Which builds record is in `config.json` (`mc.<version>.jfr`): 26.3 (Java 25) and 1.20.1 (Java 17). 1.12.2 and 1.7.10 run on Java 8, whose JFR is a backport that cannot take per-event settings on the command line;
  they are skipped with that reason in the report, never failed. A recording that is missing (game killed) or cannot be read is a non-gating check, not a failure.
- The approach (stream the recording, export only selected events, nothing from the JVM's arguments or environment) and the per-launch scratch environment are ideas from Horizon's
  `tools/JfrSummary.java`, `docs/profiling.md`, `tools/lab.py` `game_env` and `tests/test_game_home.py` (MIT licence); the code here is
  written for this repository and shares none of theirs.
- A `--jfr` run is always a partial run: profiling changes timings and RAM, so it is never a baseline.

## Safety

**What bridge-live reads outside this repository, and when.** Nothing else is read.

| When | What | How |
|---|---|---|
| every run | the checkouts and tools named in `config.json` (`repos`, `mc.<version>.dir`, `tools`, `hytale.plugin_jar`) | `git rev-parse`/`git status` for the report; the mods' own Gradle builds run in their checkouts; a sha256 of the plugin jar and of `live_peer`; size and mtime of the other binaries |
| every run | Gradle's cache (`~/.gradle`, or `GRADLE_USER_HOME`) | used by the mods' builds (`--offline`); downloaded libraries, not a game profile |
| every game launch | the path of each protected launcher folder (`~/Library/Application Support/{minecraft,PrismLauncher,Prism Launcher,PolyMC,MultiMC,ATLauncher,Hytale}`, `~/.minecraft`, `protected_dirs` in `config.json`) | resolved with `realpath` to build the sandbox's deny list and the launch guard; this looks at the folder's own entry, never inside it |
| the Hytale pairs | the installed Hytale package (`hytale.package`): its Java runtime, `HytaleServer.jar`, `Assets.zip` | read by the headless server (declared read-only, writes denied); `bridge-guest --provider hytale` reads the same install as a tool |
| only with `--minecraft-assets DIR` or `BRIDGE_LIVE_MINECRAFT_ASSETS=DIR` | `DIR` | copied once (`cp -c`) into `work/cache/minecraft-assets`, when that cache does not exist yet; never written |
| only with `--check-profiles` | the protected launcher folders above | every file's path, size and mtime, before and after the run (no file is opened) |

Without the two options, no launcher folder (Minecraft, Prism, Hytale's user data, ...) is listed or read, and the asset cache starts empty.

- Everything binds `127.0.0.1` only (that is how the mods and peers work); ports come from 47901 upwards, one pair at a time.
- Nothing is deleted with `rm` by the tool. Deletion goes through `safe_delete`, which accepts only an absolute path strictly inside an allowed root (the scratch directory `work/scratch`, or the `saves`
  folder of a dev run for a world this run created in a checkout's `run/`), refuses `..`, glob characters and symlinks.
- **What is written outside this repository.** Everything else a run writes is under `work/`, `logs/` and `reports/` of this repository.

  | Where | What | When |
  |---|---|---|
  | each mod checkout | `build/`, `.gradle/` and the other output of its own Gradle build | every Minecraft pair (the checkout's `runClient`) |
  | the checkout's git-ignored dev `run/` | `options.txt` (sound off): written by the checkout's own `prepareRunOptions` before every `runClient`, and by bridge-live for the builds without a scratch instance (26.3 Forge, 1.20.1 Forge and NeoForge, 1.12.2, 1.7.10) | every Minecraft pair |
  | the checkout's `run/config/minecraft-link.json` | `token_auth` and `endpoint_file` (never a token); other keys are kept, an unreadable file is refused, never replaced | builds without a scratch instance: token pairs, and a plain pair only when the file already exists (to switch `token_auth` off) |
  | the checkout's `run/saves/` | `linktest-<epoch>` worlds, deleted by `safe_delete` after the pair (1.12.2 and 1.7.10 reuse one fixed world) | builds without a scratch instance |
  | the checkout's `run/logs/` | the game's `latest.log` and `debug.log` | builds without a scratch instance, and 26.3 NeoForge, whose run task keeps `run/` as its process directory |
  | `~/.gradle` (or `GRADLE_USER_HOME`) | Gradle's caches | the mods' builds (`--offline`) |

  With a scratch instance (26.3 Fabric and NeoForge, 1.20.1 Fabric) the sandbox denies every other write into the checkout's `run/`. `bridge-verify` is not touched.
- Endpoint files live in a mode 0700 folder inside the pair's scratch folder.
- The Hytale server runs in `work/hytale-server` of this repository (offline auth, bound to `127.0.0.1:5529`, `-Xmx2g`), never in `hytale-link/test-server`.

## Layout

```
live                 the command
config.json          the machine's JDKs, checkouts, limits (the only place with local paths; config.local.json, git-ignored, overrides it)
orchestrator/        live.py (CLI and run), matrix.py (the pairs), mc.py (clients, scripts, options.txt), peers.py,
                     harness.py (one pair's context, start_game/start_tool, screenshots), gameenv.py (scratch home, profile guard, sandbox),
                     jfr.py (JFR options and summary), common.py, report.py
tools/JfrSummary.java     streaming JFR summariser (JDK only)
tests/               python3 -m unittest discover -s tests (fakes only: no game, no peer, no build)
tools/check_public_tree.py  the public-tree gate (no game files, decompiled code, secrets); CI runs it with the tests
peer/                live_peer, the scripted Rust peer (src/raster.rs is copied from rewrite-bridge's examples, MIT OR Apache-2.0)
tools/screenstate.swift   locked-screen detection and window lookup (CoreGraphics)
reports/ logs/ work/ output, all git-ignored: reports and logs stay on the machine that ran them
```

## License

MIT OR Apache-2.0, at your option (`LICENSE-MIT`, `LICENSE-APACHE`). See `AGENTS.md` for the CI commands and the project's rules.
