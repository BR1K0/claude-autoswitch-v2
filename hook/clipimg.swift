// Clipboard image helper for the Autoswitch hook. Build: swiftc -O clipimg.swift -o clipimg
//   clipimg info   -> {"count": <pasteboard changeCount>, "image": bool}
//   clipimg ocr    -> {"count":..., "image": bool, "text": "<recognised text>", "ms": n}
// Local only: macOS Vision text recognition, no network.
import AppKit
import Vision

let pb = NSPasteboard.general
let types: [NSPasteboard.PasteboardType] = [.png, .tiff, NSPasteboard.PasteboardType("public.jpeg")]
let data = types.lazy.compactMap { pb.data(forType: $0) }.first
var out: [String: Any] = ["count": pb.changeCount, "image": data != nil]
if CommandLine.arguments.count > 1, CommandLine.arguments[1] == "ocr", let d = data,
   let rep = NSBitmapImageRep(data: d), let cg = rep.cgImage {
    let t0 = Date()
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = CommandLine.arguments.contains("--accurate") ? .accurate : .fast
    req.usesLanguageCorrection = false
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])
    let lines = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    out["text"] = lines.joined(separator: "\n")
    out["ms"] = Int(Date().timeIntervalSince(t0) * 1000)
}
let j = try! JSONSerialization.data(withJSONObject: out)
print(String(data: j, encoding: .utf8)!)
