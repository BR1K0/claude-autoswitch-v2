// Typeahead reader for the `predictive` speed: watches the Claude desktop app's Code-tab prompt box and, whenever
// the draft settles, hands it to `typeahead.py draft`, which may queue a switch-only request for the Watcher.
// Read-only: it never types, clicks or changes focus. Started by `typeahead.py spawn` (from watch.py), so it runs
// under the Watcher's own process tree and needs no Accessibility grant of its own.
// usage: typeahead <python3> <typeahead.py>
import Cocoa
import ApplicationServices
import Foundation

let args = CommandLine.arguments
guard args.count >= 3 else { FileHandle.standardError.write("usage: typeahead <python3> <typeahead.py>\n".data(using: .utf8)!); exit(2) }
let python = args[1], script = args[2]
let env = ProcessInfo.processInfo.environment
let base = (env["AUTOSWITCH_DIR"] ?? "~/.autoswitch" as String).replacingOccurrences(of: "~", with: NSHomeDirectory())
let settle = Double(env["AUTOSWITCH_TYPEAHEAD_SETTLE"] ?? "") ?? 0.35  // seconds the draft must stay unchanged
let tick = 0.1
let idRe = try! NSRegularExpression(pattern: "local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

func attr(_ e: AXUIElement, _ a: String) -> AnyObject? { var v: AnyObject?; return AXUIElementCopyAttributeValue(e, a as CFString, &v) == .success ? v : nil }
func str(_ e: AXUIElement, _ a: String) -> String { (attr(e, a) as? String) ?? "" }
func find(_ e: AXUIElement, _ d: Int = 0, _ pred: (AXUIElement) -> Bool) -> AXUIElement? {
  if d > 45 { return nil }
  if pred(e) { return e }
  for k in (attr(e, "AXChildren") as? [AXUIElement]) ?? [] { if let f = find(k, d + 1, pred) { return f } }
  return nil
}
func isPromptBox(_ e: AXUIElement) -> Bool { str(e, "AXRole") == "AXTextArea" && str(e, "AXDescription") == "Prompt" }
// The open chat's id: the chat pane is a web area whose URL is https://claude.ai/epitaxy/<session id>.
// (A sidebar row's DOM id also carries an id, but not reliably the open chat's: it named another chat in testing.)
func openChatId(_ win: AXUIElement) -> String? {
  var out: String?
  _ = find(win) { e in
    guard str(e, "AXRole") == "AXWebArea", let u = attr(e, "AXURL") as? URL else { return false }
    let s = u.absoluteString
    guard s.contains("/epitaxy/"), let m = idRe.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)), let r = Range(m.range, in: s) else { return false }
    out = String(s[r]); return true
  }
  return out
}
// The open chat's title: the level-2 heading with a description (the sidebar's own headings carry a title instead).
func openChatTitle(_ win: AXUIElement) -> String {
  guard let h = find(win, 0, { str($0, "AXRole") == "AXHeading" && (attr($0, "AXValue") as? NSNumber)?.intValue == 2 && !str($0, "AXDescription").isEmpty }) else { return "" }
  return str(h, "AXDescription")
}
func codeTab(_ win: AXUIElement) -> Bool {
  // The Code / Claude switch: when it is not found (layout without it), assume Code; the Chat tab has no hook anyway.
  guard let sel = find(win, 0, { str($0, "AXRole") == "AXRadioButton" && ["Claude", "Code"].contains(str($0, "AXDescription")) && (attr($0, "AXValue") as? NSNumber)?.intValue == 1 })
  else { return true }
  return str(sel, "AXDescription") == "Code"
}
func predictiveOn() -> Bool {  // the reader stops when the speed is changed away from predictive
  if env["AUTOSWITCH_TYPEAHEAD_DRY"] == "1" { return true }
  let v = env["AUTOSWITCH_SPEED"] ?? ((try? String(contentsOfFile: base + "/speed", encoding: .utf8)) ?? "")
  return ["predictive", "inline"].contains(v.trimmingCharacters(in: .whitespacesAndNewlines).lowercased())
}
func watcherAlive() -> Bool {
  guard let a = try? FileManager.default.attributesOfItem(atPath: base + "/watcher.alive"), let m = a[.modificationDate] as? Date else { return false }
  return Date().timeIntervalSince(m) < 90
}

var running: Process? = nil
var queued: [String: String]? = nil
func submit(_ payload: [String: String]) {
  if let p = running, p.isRunning { queued = payload; return }  // one at a time; only the newest waiting draft matters
  let p = Process()
  p.executableURL = URL(fileURLWithPath: python)
  p.arguments = [script, "draft"]
  let pipe = Pipe(); p.standardInput = pipe
  p.standardOutput = FileHandle.nullDevice; p.standardError = FileHandle.nullDevice
  p.terminationHandler = { _ in DispatchQueue.main.async { running = nil; if let q = queued { queued = nil; submit(q) } } }
  do { try p.run() } catch { return }
  running = p
  pipe.fileHandleForWriting.write(try! JSONSerialization.data(withJSONObject: payload))
  try? pipe.fileHandleForWriting.close()
}

var box: AXUIElement? = nil, boxFoundAt = Date.distantPast
var lastText = "", changedAt = Date(), handled = "", lastAliveCheck = Date()
Timer.scheduledTimer(withTimeInterval: tick, repeats: true) { _ in
  if Date().timeIntervalSince(lastAliveCheck) > 10 { lastAliveCheck = Date(); if !watcherAlive() || !predictiveOn() { exit(0) } }
  guard let app = NSWorkspace.shared.frontmostApplication, app.localizedName == "Claude" else { box = nil; return }
  let root = AXUIElementCreateApplication(app.processIdentifier)
  if box == nil || Date().timeIntervalSince(boxFoundAt) > 2 {  // re-find now and then: chats and windows change
    AXUIElementSetAttributeValue(root, "AXManualAccessibility" as CFString, true as CFTypeRef)
    guard let win = attr(root, "AXFocusedWindow") else { box = nil; return }
    box = find(win as! AXUIElement, 0, isPromptBox); boxFoundAt = Date()
  }
  guard let b = box, let text = attr(b, "AXValue") as? String else { box = nil; return }
  if text != lastText { lastText = text; changedAt = Date(); if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { handled = "" }; return }
  let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
  guard t.count >= 3, t != handled, Date().timeIntervalSince(changedAt) >= settle else { return }
  handled = t
  guard let win = attr(root, "AXFocusedWindow") else { return }
  let w = win as! AXUIElement
  guard codeTab(w), let id = openChatId(w) else { return }
  submit(["session_id": id, "text": t, "title": openChatTitle(w)])
}
RunLoop.main.run()
