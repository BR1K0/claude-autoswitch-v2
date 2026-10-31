// Text recognition for ACPMH screenshots. Build: swiftc -O ocr.swift -o acpmh-ocr
//   acpmh-ocr FILE...  -> one JSON line per file: {"file": ..., "ok": bool, "text": "..."}
// Local only: macOS Vision text recognition, no network.
import AppKit
import Vision

for path in CommandLine.arguments.dropFirst() {
    var out: [String: Any] = ["file": path, "ok": false, "text": ""]
    if let img = NSImage(contentsOfFile: path),
       let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
        let req = VNRecognizeTextRequest()
        req.recognitionLevel = .accurate
        req.usesLanguageCorrection = true
        if (try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([req])) != nil {
            out["text"] = (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }
                .joined(separator: "\n")
            out["ok"] = true
        }
    }
    let j = try! JSONSerialization.data(withJSONObject: out)
    print(String(data: j, encoding: .utf8)!)
}
