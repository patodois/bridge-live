// screenstate: prints one JSON line about the console session and, with --windows PID..., the on-screen
// windows owned by those processes. Read-only: it only asks the window server what exists.
//   screenstate                      -> {"locked":false,"display_asleep":false,"on_console":true}
//   screenstate --windows 123 456    -> adds "windows":[{"id":N,"pid":P,"owner":"java","title":"...","w":W,"h":H,"x":X,"y":Y}]
import Foundation
import CoreGraphics

let args = Array(CommandLine.arguments.dropFirst())
var pids = Set<Int32>()
if let i = args.firstIndex(of: "--windows") {
    for a in args[(i + 1)...] { if let p = Int32(a) { pids.insert(p) } }
}
var out: [String: Any] = [:]
let dict = CGSessionCopyCurrentDictionary() as? [String: Any] ?? [:]
out["locked"] = (dict["CGSSessionScreenIsLocked"] as? Bool) ?? ((dict["CGSSessionScreenIsLocked"] as? Int ?? 0) != 0)
out["on_console"] = (dict["kCGSSessionOnConsoleKey"] as? Bool) ?? true
out["display_asleep"] = CGDisplayIsAsleep(CGMainDisplayID()) != 0
if !pids.isEmpty {
    var ws: [[String: Any]] = []
    let info = CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID) as? [[String: Any]] ?? []
    for w in info {
        guard let pid = w[kCGWindowOwnerPID as String] as? Int32, pids.contains(pid) else { continue }
        let b = w[kCGWindowBounds as String] as? [String: Any] ?? [:]
        ws.append([
            "id": w[kCGWindowNumber as String] as? Int ?? 0,
            "pid": Int(pid),
            "owner": w[kCGWindowOwnerName as String] as? String ?? "",
            "title": w[kCGWindowName as String] as? String ?? "",
            "layer": w[kCGWindowLayer as String] as? Int ?? 0,
            "x": b["X"] as? Double ?? 0, "y": b["Y"] as? Double ?? 0,
            "w": b["Width"] as? Double ?? 0, "h": b["Height"] as? Double ?? 0,
        ])
    }
    out["windows"] = ws
}
let data = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
