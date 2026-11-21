//! live_peer: a scripted passthrough-link peer for the live matrix of bridge-live.
//!
//!   live_peer guest --port P --label L [--seconds 14] [--frames] [--entity 1] [--expect-cameras N]
//!                   [--expect-frames-drawn-by-host] [--connect-timeout 100] [--endpoint-file PATH] [--expect-refused]
//!   live_peer host  --port P --label L [--seconds 14] [--entity 1] [--expect-poses N] [--expect-spawns N]
//!                   [--expect-moved M] [--guest-leaves] [--connect-timeout 100] [--endpoint-file PATH] [--expect-refused]
//!
//! `--endpoint-file PATH` (contract section 6.2, since revision 6): a host writes the file after binding (with a token of its own making,
//! which this program never prints) and removes it when it ends; a guest reads the port and the token from it before every attempt.
//! `--expect-refused`: the run passes only if the session was refused with `unauthorized` (a guest: `peer said bye: unauthorized`) and
//! never connected; it ends as soon as the refusal has been seen.
//!
//! `--guest-leaves` (host): the guest ends the session (despawn, bye) and `--seconds` is only the longest wait for that.
//!
//! As a **guest** it connects to a real game's host (retrying until one answers), spawns one entity (a lit
//! cube), and for every `Camera` it receives places the cube 4 m ahead of the first camera, on the host's
//! collision ground once a snapshot covers it, sends the pose and, with `--frames`, renders the cube with that
//! exact camera on the CPU (colour plus linear depth, 320x240) into a pixel file and answers with a `Frame`.
//! After `--seconds` it despawns, says bye and prints its result.
//!
//! As a **host** it listens, spawns one entity (a tank stand-in that circles 3 m ahead of the guest's first
//! pose, or the origin if the guest never posed), sweeps a camera, sends a ground collision snapshot every
//! second, grants the guest the input focus when it asks (and takes it back 2 s later), records everything the
//! guest sends, and after `--seconds` despawns, says bye and prints its result.
//!
//! The last stdout line is `RBL-INTEROP-RESULT {json}` with the keys of rewrite-bridge's tests/interop peers
//! plus a few more (the `extra` object). Exit status 0 when every check passed, 1 otherwise, 2 for a bad command line.

mod raster;

use std::path::PathBuf;
use std::process::exit;
use std::thread::sleep;
use std::time::{Duration, Instant};

use raster::{cuboid, lit, Target};
use rewrite_bridge::link::*;
use serde_json::json;

const SIZE: (u32, u32) = (320, 240);
const CUBE_HALF: f32 = 0.8;
const IN_FRONT: f32 = 4.0;
/// Seconds before the despawn when poses stop: a pose still in flight (UDP) must not arrive after the despawn (TCP), which a receiver counts as unknown_entity.
const QUIET: f32 = 0.3;

fn arg<T: std::str::FromStr>(name: &str) -> Option<T> {
    let args: Vec<String> = std::env::args().collect();
    let i = args.iter().position(|a| a == name)?;
    args.get(i + 1)?.parse().ok()
}

fn flag(name: &str) -> bool {
    std::env::args().any(|a| a == name)
}

#[derive(Default)]
struct Rec {
    failures: Vec<String>,
    peer_game: Option<String>,
    protocol: Option<u16>,
    session: Option<u32>,
    spawns: Vec<u32>,
    despawns: Vec<u32>,
    /// The library's unknown_entity count when the first despawn arrived: a pose sent (UDP) just before a
    /// despawn (TCP) can arrive after it; the contract has the receiver drop it, so only earlier ones are a failure.
    unknown_at_despawn: Option<u64>,
    cameras: u32,
    bad_cameras: u32,
    poses: u32,
    collision_triangles: usize,
    collisions: u32,
    frames_ok: u32,
    frames_bad: u32,
    frames_sent: u32,
    poses_sent: u32,
    focus: Vec<String>,
    lost_reason: Option<String>,
    handshake_failed: Option<String>,
    /// Handshakes that ended with `unauthorized` (contract section 6.2).
    refused: u32,
    first_pose: Option<[f32; 3]>,
    last_pose: Option<[f32; 3]>,
    max_moved: f32,
    depth_finite_max: usize,
}

impl Rec {
    fn fail(&mut self, msg: impl Into<String>) {
        self.failures.push(msg.into());
    }
}

fn dist(a: [f32; 3], b: [f32; 3]) -> f32 {
    ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2) + (a[2] - b[2]).powi(2)).sqrt()
}

fn camera_ok(c: &Camera) -> bool {
    c.view.iter().chain(c.proj.iter()).all(|v| v.is_finite())
        && c.viewport[0] > 0
        && c.viewport[1] > 0
        && c.near > 0.0
        && c.fov_y > 0.0
}

fn render(camera: &Camera, center: [f32; 3], yaw: f32) -> Target {
    let mut target = Target::new(SIZE.0 as usize, SIZE.1 as usize);
    target.draw(camera, &cuboid(center, [CUBE_HALF; 3], yaw), &|_, n| {
        let base = [[235.0, 80.0, 70.0], [70.0, 200.0, 110.0], [80.0, 130.0, 240.0]];
        let (axis, sign) = (0..3)
            .map(|a| (a, n[a]))
            .max_by(|x, y| x.1.abs().total_cmp(&y.1.abs()))
            .unwrap();
        let mut c = base[axis];
        if sign < 0.0 {
            c = c.map(|v| v * 0.7 + 60.0);
        }
        lit(c, n)
    });
    target
}

fn ahead_of(camera: &Camera) -> [f32; 3] {
    let (eye, f) = (camera.eye(), camera.forward());
    let len = (f[0] * f[0] + f[2] * f[2]).sqrt();
    let (dx, dz) = if len > 1e-3 { (f[0] / len, f[2] / len) } else { (0.0, -1.0) };
    [eye[0] + dx * IN_FRONT, eye[1], eye[2] + dz * IN_FRONT]
}

fn ground_under(triangles: &[Triangle], x: f32, z: f32, max_y: f32) -> Option<f32> {
    triangles
        .iter()
        .filter_map(|[a, b, c]| {
            let det = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2]);
            if det.abs() < 1e-9 {
                return None;
            }
            let u = ((b[2] - c[2]) * (x - c[0]) + (c[0] - b[0]) * (z - c[2])) / det;
            let v = ((c[2] - a[2]) * (x - c[0]) + (a[0] - c[0]) * (z - c[2])) / det;
            let w = 1.0 - u - v;
            (u >= 0.0 && v >= 0.0 && w >= 0.0).then(|| u * a[1] + v * b[1] + w * c[1])
        })
        .filter(|&y| y <= max_y)
        .max_by(f32::total_cmp)
}

struct Opts {
    port: u16,
    label: String,
    seconds: f32,
    connect_timeout: f32,
    entity: u32,
    frames: bool,
    expect_cameras: u32,
    expect_poses: u32,
    expect_spawns: u32,
    expect_moved: f32,
    expect_collision: bool,
    guest_leaves: bool,
    endpoint_file: Option<PathBuf>,
    expect_refused: bool,
}

/// The refusal of a host that wants a token (contract section 6.2), as the host (`unauthorized`) and the guest (`peer said bye: unauthorized`) report it.
fn is_unauthorized(reason: &str) -> bool {
    reason == UNAUTHORIZED || reason == format!("peer said bye: {UNAUTHORIZED}")
}

fn log(start: Instant, role: &str, msg: String) {
    println!("[live-{role} {:6.2}s] {msg}", start.elapsed().as_secs_f32());
}

fn note_connected(r: &mut Rec, p: &PeerInfo, want: Role) {
    r.peer_game = Some(p.game.clone());
    r.protocol = Some(p.protocol);
    r.session = Some(p.session);
    if p.role != want {
        r.fail(format!("peer role is {:?}, expected {:?}", p.role, want));
    }
    if p.protocol != 1 {
        r.fail(format!("negotiated protocol {} (expected 1)", p.protocol));
    }
}

fn run_guest(o: &Opts, r: &mut Rec, start: Instant) -> Stats {
    let mut link = Link::guest(LinkConfig {
        control_port: o.port,
        endpoint_file: o.endpoint_file.clone(),
        ..LinkConfig::new(format!("Live Peer ({})", o.label))
    })
        .unwrap_or_else(|e| {
            eprintln!("cannot start the guest: {e}");
            exit(2)
        });
    log(start, "guest", format!("connecting to 127.0.0.1:{}", o.port));
    let mut writer: Option<FrameWriter> = None;
    let mut center: Option<[f32; 3]> = None;
    let mut grounded = false;
    let mut last_collision: Vec<Triangle> = Vec::new();
    let mut t0: Option<f32> = None;
    let mut ask_focus_at: Option<f32> = None;
    let mut spawned = false;
    let mut ending: Option<Instant> = None;
    let mut next_report = 1.0;
    let (mut cams_win, mut poses_win, mut frames_win) = (0, 0, 0);
    // The counters as they stood at the last poll of a live session: a heartbeat still in flight when the session ends is
    // legitimately "foreign" to a link that is no longer in one (tests/interop/README.md), so the end-of-run read does not count.
    let mut snap = Stats::default();
    loop {
        let t = start.elapsed().as_secs_f32();
        if o.expect_refused && r.refused > 0 {
            // refused, as the run expected: let the closing drain, then report
            let until = Instant::now() + Duration::from_millis(400);
            while Instant::now() < until {
                let _ = link.poll();
                sleep(Duration::from_millis(5));
            }
            break;
        }
        if t0.is_none() && t > o.connect_timeout {
            r.fail(format!("no host answered within {} s", o.connect_timeout));
            break;
        }
        let mut newest: Option<(u32, Camera)> = None;
        for event in link.poll() {
            match event {
                Event::Connected(p) => {
                    log(start, "guest", format!("connected: host \"{}\", protocol v{}, session {:#010x}", p.game, p.protocol, p.session));
                    note_connected(r, &p, Role::Host);
                    t0 = Some(t);
                    let _ = link.spawn(&Spawn {
                        entity: o.entity,
                        provider: "Live Peer".into(),
                        asset: "demo/cube".into(),
                        name: Some("Cube".into()),
                    });
                    spawned = true;
                    if o.frames {
                        let tag = format!("live-{:08x}", p.session);
                        let opts = FrameFileOptions::new(SIZE.0, SIZE.1, 3).with_depth();
                        match FrameWriter::create_temp_with(&tag, &opts) {
                            Ok(wr) => {
                                let _ = link.announce_frames(wr.channel());
                                log(start, "guest", format!("pixel file {} ({}x{}, depth: true)", wr.channel().path, SIZE.0, SIZE.1));
                                writer = Some(wr);
                            }
                            Err(e) => r.fail(format!("pixel file: {e}")),
                        }
                    }
                    ask_focus_at = Some(t + 2.0);
                }
                Event::Lost { reason } => {
                    log(start, "guest", format!("LOST the host: {reason}"));
                    if ending.is_none() {
                        r.lost_reason = Some(reason);
                        r.fail("the host ended the session before this peer did");
                    }
                    writer = None;
                    ending.get_or_insert(Instant::now() - Duration::from_secs(10));
                }
                Event::HandshakeFailed { reason } => {
                    log(start, "guest", format!("handshake failed: {reason}"));
                    if o.expect_refused && is_unauthorized(&reason) {
                        r.refused += 1;
                    } else {
                        r.handshake_failed = Some(reason);
                    }
                }
                Event::Camera { header, camera } => {
                    r.cameras += 1;
                    cams_win += 1;
                    if !camera_ok(&camera) {
                        r.bad_cameras += 1;
                    }
                    newest = Some((header.seq, camera));
                }
                Event::Collision(c) => {
                    r.collisions += 1;
                    r.collision_triangles = c.triangles.len();
                    log(start, "guest", format!("collision query {}: {} triangles within {} m of {:?}", c.query, c.triangles.len(), c.radius, c.center));
                    last_collision = c.triangles;
                }
                Event::Focus { focus, requested } => {
                    r.focus.push(format!("{}:{}", focus.as_str(), if requested { "request" } else { "set" }));
                    log(start, "guest", format!("host says input focus is now with the {}", focus.as_str()));
                }
                Event::Spawn(s) => r.spawns.push(s.entity),
                Event::Despawn { entity } => {
                    r.unknown_at_despawn.get_or_insert(link.stats().unknown_entity);
                    r.despawns.push(entity);
                }
                Event::Poses { poses, .. } => r.poses += poses.len() as u32,
                _ => {}
            }
        }
        if let Some((_, camera)) = &newest {
            if center.is_none() {
                let at = ahead_of(camera);
                log(start, "guest", format!("cube placed {IN_FRONT} m ahead of the camera at {:?}", at.map(|v| (v * 100.0).round() / 100.0)));
                center = Some(at);
            }
        }
        if let (Some(at), false) = (center, grounded) {
            if let Some(y) = ground_under(&last_collision, at[0], at[2], at[1] + 0.5) {
                center = Some([at[0], y + CUBE_HALF, at[2]]);
                grounded = true;
                log(start, "guest", format!("cube stands on the host's ground at y {y:.2}"));
            }
        }
        let live = ending.is_none() && t0.is_some_and(|t0| t - t0 < o.seconds - QUIET);
        if let (Some((seq, camera)), Some(center), true) = (newest, center, live) {
            let now = now_us();
            let yaw = 0.6 * t;
            let mut pose = Pose::new(o.entity, now);
            pose.translation = center;
            pose.rotation = [0.0, (yaw * 0.5).sin(), 0.0, (yaw * 0.5).cos()];
            if link.send_poses(&[pose]).is_ok() {
                r.poses_sent += 1;
                poses_win += 1;
            }
            if let Some(wr) = &mut writer {
                let target = render(&camera, center, yaw);
                let finite = target.depth.iter().filter(|d| d.is_finite()).count();
                r.depth_finite_max = r.depth_finite_max.max(finite);
                let depth = wr.has_depth().then_some(target.depth.as_slice());
                if let Ok(ready) = wr.write_depth(&target.rgba, depth, SIZE.0, SIZE.1, seq, now) {
                    if link.send_frame(&ready).is_ok() {
                        r.frames_sent += 1;
                        frames_win += 1;
                    }
                }
            }
        }
        if ask_focus_at.is_some_and(|a| t >= a) {
            ask_focus_at = None;
            let _ = link.set_focus(Role::Guest);
            log(start, "guest", "asked the host for input focus".into());
        }
        // End of the scripted session: despawn, bye, linger so the closing drains.
        if let (Some(t0), None) = (t0, ending) {
            if t - t0 >= o.seconds && spawned {
                let _ = link.despawn(o.entity);
                log(start, "guest", "despawn, then bye".into());
                ending = Some(Instant::now());
            }
        }
        if let Some(e) = ending {
            let waited = e.elapsed();
            if waited >= Duration::from_millis(150) && link.is_connected() {
                link.disconnect("done");
                // after disconnect() keep polling for the linger time
                let until = Instant::now() + Duration::from_millis(500);
                while Instant::now() < until {
                    let _ = link.poll();
                    sleep(Duration::from_millis(5));
                }
                break;
            }
            if !link.is_connected() && waited >= Duration::from_millis(150) {
                break;
            }
        }
        if link.is_connected() {
            snap = link.stats();
        }
        if t >= next_report {
            next_report += 1.0;
            if link.is_connected() {
                log(start, "guest", format!("got {cams_win} cameras; sent {poses_win} poses, {frames_win} frames; focus with the {}", link.focus().as_str()));
            }
            (cams_win, poses_win, frames_win) = (0, 0, 0);
        }
        sleep(Duration::from_millis(2));
    }
    snap
}

fn run_host(o: &Opts, r: &mut Rec, start: Instant) -> Stats {
    let mut link = Link::host(LinkConfig {
        control_port: o.port,
        endpoint_file: o.endpoint_file.clone(),
        ..LinkConfig::new(format!("Live Peer ({})", o.label))
    })
        .unwrap_or_else(|e| {
            eprintln!("cannot start the host: {e}");
            exit(2)
        });
    log(start, "host", format!("listening on {}", link.control_addr().unwrap()));
    let mut reader: Option<FrameReader> = None;
    let (mut pixels, mut depth) = (Vec::new(), Vec::new());
    let mut t0: Option<f32> = None;
    let mut next_collision = 0.0f32;
    let mut give_back_at: Option<f32> = None;
    let mut anchor: Option<[f32; 3]> = None;
    let mut next_pose = 0.0f32;
    let mut ending: Option<Instant> = None;
    let mut next_report = 1.0;
    let (mut cams_win, mut poses_win) = (0, 0);
    let mut sent_cameras = 0u32;
    let mut snap = Stats::default(); // see run_guest
    loop {
        let t = start.elapsed().as_secs_f32();
        if o.expect_refused && r.refused > 0 {
            let until = Instant::now() + Duration::from_millis(400);
            while Instant::now() < until {
                let _ = link.poll();
                sleep(Duration::from_millis(5));
            }
            break;
        }
        if t0.is_none() && t > o.connect_timeout {
            r.fail(format!("no guest connected within {} s", o.connect_timeout));
            break;
        }
        for event in link.poll() {
            match event {
                Event::Connected(p) => {
                    log(start, "host", format!("connected: guest \"{}\", protocol v{}, session {:#010x}", p.game, p.protocol, p.session));
                    note_connected(r, &p, Role::Guest);
                    t0 = Some(t);
                    next_collision = t;
                    let _ = link.spawn(&Spawn {
                        entity: o.entity,
                        provider: "Live Peer".into(),
                        asset: "demo/tank".into(),
                        name: Some("Live Tank".into()),
                    });
                }
                Event::Lost { reason } => {
                    log(start, "host", format!("LOST the guest: {reason}"));
                    if ending.is_none() && !o.guest_leaves {
                        r.fail("the guest ended the session before this peer did");
                    }
                    r.lost_reason = Some(reason);
                    reader = None;
                    ending.get_or_insert(Instant::now() - Duration::from_secs(10));
                }
                Event::HandshakeFailed { reason } => {
                    log(start, "host", format!("handshake failed: {reason}"));
                    if o.expect_refused && is_unauthorized(&reason) {
                        r.refused += 1;
                    } else {
                        r.handshake_failed = Some(reason);
                    }
                }
                Event::Spawn(s) => {
                    log(start, "host", format!("guest spawned entity {} = {} asset \"{}\"", s.entity, s.provider, s.asset));
                    r.spawns.push(s.entity);
                }
                Event::Despawn { entity } => {
                    log(start, "host", format!("guest despawned entity {entity}"));
                    r.unknown_at_despawn.get_or_insert(link.stats().unknown_entity);
                    r.despawns.push(entity);
                }
                Event::Poses { poses: ps, .. } => {
                    r.poses += ps.len() as u32;
                    poses_win += ps.len();
                    for p in ps {
                        if r.first_pose.is_none() {
                            r.first_pose = Some(p.translation);
                            anchor = Some(p.translation);
                        }
                        r.last_pose = Some(p.translation);
                        if let Some(f) = r.first_pose {
                            r.max_moved = r.max_moved.max(dist(f, p.translation));
                        }
                    }
                }
                Event::Focus { focus, requested: true } => {
                    r.focus.push(format!("{}:request", focus.as_str()));
                    log(start, "host", format!("guest asks for input focus; granting it to the {}", focus.as_str()));
                    let _ = link.set_focus(focus);
                    give_back_at = Some(t + 2.0);
                }
                Event::Focus { focus, requested } => {
                    r.focus.push(format!("{}:{}", focus.as_str(), if requested { "request" } else { "set" }));
                }
                Event::FrameChannel(c) => {
                    reader = FrameReader::open(c.path.as_ref()).ok();
                    log(start, "host", format!("guest pixel file {} ({}x{}, {} slots)", c.path, c.width, c.height, c.slots));
                }
                Event::Frame { frame, .. } => {
                    if let Some(rd) = &reader {
                        match rd.read_depth(&frame, &mut pixels, &mut depth) {
                            Ok(_) => r.frames_ok += 1,
                            Err(_) => r.frames_bad += 1,
                        }
                    } else {
                        r.frames_bad += 1;
                    }
                }
                _ => {}
            }
        }
        if link.is_connected() && ending.is_none() {
            let cam = Camera::look_at(
                [1.4 * (0.9 * t).sin(), 1.8, 5.5],
                [0.0, 0.9, 0.0],
                1.0,
                0.1,
                f32::INFINITY,
                [1280, 720],
            )
            .with_depth_range(DepthRange::Reversed);
            if link.send_camera(&cam).is_ok() {
                sent_cameras += 1;
                cams_win += 1;
            }
            if t >= next_collision {
                next_collision = t + 1.0;
                let mut tris: Vec<Triangle> = Vec::new();
                for i in -10..10 {
                    for j in -10..10 {
                        let (x, z) = (i as f32, j as f32);
                        tris.push([[x, 0.0, z], [x, 0.0, z + 1.0], [x + 1.0, 0.0, z + 1.0]]);
                        tris.push([[x, 0.0, z], [x + 1.0, 0.0, z + 1.0], [x + 1.0, 0.0, z]]);
                    }
                }
                let _ = link.send_collision([0.0, 0.0, 0.0], 10.0, &tris);
            }
            if give_back_at.is_some_and(|g| t >= g) {
                give_back_at = None;
                let _ = link.set_focus(Role::Host);
                log(start, "host", "took the input focus back".into());
            }
            // The host's own entity: circles 3 m ahead of the guest's first pose (or the origin) at 30 Hz.
            if t >= next_pose && t0.is_some_and(|t0| t - t0 < o.seconds - QUIET) {
                next_pose = t + 1.0 / 30.0;
                let a = anchor.unwrap_or([0.0, 0.0, 0.0]);
                let ang = 0.8 * t;
                let mut pose = Pose::new(o.entity, now_us());
                pose.translation = [a[0] + 3.0 * ang.cos(), a[1], a[2] + 3.0 + 3.0 * ang.sin()];
                pose.velocity = [-3.0 * 0.8 * ang.sin(), 0.0, 3.0 * 0.8 * ang.cos()];
                if t0.is_some_and(|t0| (t - t0) < 0.1) {
                    pose.flags = POSE_TELEPORT;
                }
                let _ = link.send_poses(&[pose]);
            }
        }
        if let (Some(t0), None) = (t0, ending) {
            if t - t0 >= o.seconds {
                if o.guest_leaves {
                    r.fail(format!("the guest did not leave within {} s", o.seconds));
                }
                let _ = link.despawn(o.entity);
                log(start, "host", "despawn, then bye".into());
                ending = Some(Instant::now());
            }
        }
        if let Some(e) = ending {
            let waited = e.elapsed();
            if waited >= Duration::from_millis(300) && link.is_connected() {
                link.disconnect("done");
                let until = Instant::now() + Duration::from_millis(500);
                while Instant::now() < until {
                    let _ = link.poll();
                    sleep(Duration::from_millis(5));
                }
                break;
            }
            if !link.is_connected() && waited >= Duration::from_millis(150) {
                break;
            }
        }
        if link.is_connected() {
            snap = link.stats();
        }
        if t >= next_report {
            next_report += 1.0;
            if link.is_connected() {
                let s = link.stats();
                log(start, "host", format!(
                    "sent {cams_win} cameras; got {poses_win} poses; last pose {:?}; malformed {} stale {} foreign {} unknown_entity {}",
                    r.last_pose.map(|p| p.map(|x| (x * 100.0).round() / 100.0)),
                    s.malformed, s.stale, s.foreign, s.unknown_entity
                ));
            }
            (cams_win, poses_win) = (0, 0);
        }
        sleep(Duration::from_millis(16));
    }
    r.cameras = sent_cameras; // the host's "cameras" is what it sent (it receives none)
    snap
}

fn main() {
    let role = std::env::args().nth(1);
    let role_s = role.as_deref().unwrap_or("");
    if role_s != "host" && role_s != "guest" {
        eprintln!("usage: live_peer host|guest --port PORT --label LABEL [--seconds S] [--frames] [--entity N] [expectations]");
        exit(2);
    }
    let Some(port) = arg::<u16>("--port") else {
        eprintln!("--port is required");
        exit(2)
    };
    let o = Opts {
        port,
        label: arg("--label").unwrap_or_else(|| "live".into()),
        seconds: arg("--seconds").unwrap_or(14.0),
        connect_timeout: arg("--connect-timeout").unwrap_or(100.0),
        entity: arg("--entity").unwrap_or(1),
        frames: flag("--frames"),
        expect_cameras: arg("--expect-cameras").unwrap_or(0),
        expect_poses: arg("--expect-poses").unwrap_or(0),
        expect_spawns: arg("--expect-spawns").unwrap_or(0),
        expect_moved: arg("--expect-moved").unwrap_or(0.0),
        expect_collision: flag("--expect-collision"),
        guest_leaves: flag("--guest-leaves"),
        endpoint_file: arg::<String>("--endpoint-file").map(PathBuf::from),
        expect_refused: flag("--expect-refused"),
    };
    let start = Instant::now();
    let mut r = Rec::default();
    let stats = if role_s == "guest" { run_guest(&o, &mut r, start) } else { run_host(&o, &mut r, start) };

    // Checks every run makes.
    if o.expect_refused {
        if r.session.is_some() {
            r.fail("connected although the token was wrong");
        }
        if r.refused == 0 {
            r.fail("was never refused with unauthorized");
        }
    } else if r.session.is_none() {
        r.fail("never connected");
    }
    if let Some(reason) = &r.handshake_failed {
        r.fail(format!("handshake failed: {reason}"));
    }
    if stats.malformed != 0 {
        r.fail(format!("{} malformed datagrams", stats.malformed));
    }
    let before_despawn = r.unknown_at_despawn.unwrap_or(stats.unknown_entity);
    if before_despawn != 0 {
        r.fail(format!("{before_despawn} poses for an entity that was never spawned"));
    }
    if stats.spawn_ignored != 0 {
        r.fail(format!("{} spawns ignored", stats.spawn_ignored));
    }
    if r.bad_cameras != 0 {
        r.fail(format!("{} cameras with non-finite values or an empty viewport", r.bad_cameras));
    }
    // Checks the caller asked for.
    if o.expect_refused {
        // nothing else to expect from a session that never was
    } else if role_s == "guest" {
        if r.cameras < o.expect_cameras {
            r.fail(format!("cameras received: {} (expected at least {})", r.cameras, o.expect_cameras));
        }
        if o.expect_collision && r.collisions == 0 {
            r.fail("no collision snapshot received");
        }
        if o.frames && r.frames_sent == 0 {
            r.fail("no frame was sent");
        }
    } else {
        if r.poses < o.expect_poses {
            r.fail(format!("poses received: {} (expected at least {})", r.poses, o.expect_poses));
        }
        if (r.spawns.len() as u32) < o.expect_spawns {
            r.fail(format!("spawns received: {} (expected at least {})", r.spawns.len(), o.expect_spawns));
        }
        if o.guest_leaves {
            match &r.lost_reason {
                Some(x) if x.contains("bye") => {}
                other => r.fail(format!("lost reason {other:?} does not mention bye")),
            }
        }
        if r.max_moved < o.expect_moved {
            r.fail(format!("the guest's pose moved {:.2} m (expected at least {:.2})", r.max_moved, o.expect_moved));
        }
    }
    let ok = r.failures.is_empty();
    let result = json!({
        "role": role_s, "label": o.label, "ok": ok, "failures": r.failures,
        "peer_game": r.peer_game, "protocol": r.protocol, "session": r.session,
        "spawns": r.spawns, "despawns": r.despawns, "cameras": r.cameras, "poses": r.poses,
        "collision_triangles": r.collision_triangles, "frames_ok": r.frames_ok, "frames_bad": r.frames_bad,
        "focus": r.focus, "lost_reason": r.lost_reason,
        "stats": {"malformed": stats.malformed, "foreign": stats.foreign, "stale": stats.stale,
                  "unknown_entity": stats.unknown_entity, "spawn_ignored": stats.spawn_ignored},
        "extra": {
            "poses_sent": r.poses_sent, "frames_sent": r.frames_sent, "collisions": r.collisions,
            "first_pose": r.first_pose, "last_pose": r.last_pose, "max_moved_m": r.max_moved,
            "depth_finite_pixels_max": r.depth_finite_max,
            "datagrams_sent": stats.datagrams_sent, "datagrams_received": stats.datagrams_received,
            "lines_sent": stats.lines_sent, "lines_received": stats.lines_received,
            "send_dropped": stats.send_dropped, "seconds": start.elapsed().as_secs_f32(),
            "rewrite_bridge": env!("LIVE_PEER_REWRITE_BRIDGE_VERSION"),
            "rewrite_bridge_source": env!("LIVE_PEER_REWRITE_BRIDGE_SOURCE"),
            "refused": r.refused,
        },
    });
    println!("RBL-INTEROP-RESULT {result}");
    exit(if ok { 0 } else { 1 });
}
