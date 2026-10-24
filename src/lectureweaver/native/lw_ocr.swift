// Local OCR with Apple Vision (zh-Hans + en). No network.
// usage: lw_ocr [--fast] [--no-correction] <image>...
// prints one JSON object per image: {"file":..., "w":..., "h":..., "lines":[{"text":..., "conf":..., "box":[x,y,w,h]}]}
// Boxes are normalised to the image, origin top-left.
import Foundation
import Vision
import ImageIO
import PDFKit
import AppKit

var fast = false
var correction = true
var pdfMode = false
var files: [String] = []
for a in CommandLine.arguments.dropFirst() {
    if a == "--fast" { fast = true } else if a == "--no-correction" { correction = false } else if a == "--pdf" { pdfMode = true } else { files.append(a) }
}

func jsonString(_ s: String) -> String {
    let data = try! JSONSerialization.data(withJSONObject: [s], options: [])
    let str = String(data: data, encoding: .utf8)!
    return String(str.dropFirst().dropLast())
}

func recognize(_ img: CGImage, label f: String) {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = fast ? .fast : .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = correction
    let handler = VNImageRequestHandler(cgImage: img, options: [:])
    do { try handler.perform([req]) } catch {
        print("{\"file\":\(jsonString(f)),\"error\":\(jsonString(error.localizedDescription))}")
        return
    }
    var parts: [String] = []
    for obs in req.results ?? [] {
        guard let top = obs.topCandidates(1).first else { continue }
        let b = obs.boundingBox
        let box = String(format: "[%.4f,%.4f,%.4f,%.4f]", b.origin.x, 1 - b.origin.y - b.size.height, b.size.width, b.size.height)
        parts.append("{\"text\":\(jsonString(top.string)),\"conf\":\(String(format: "%.3f", top.confidence)),\"box\":\(box)}")
    }
    print("{\"file\":\(jsonString(f)),\"w\":\(img.width),\"h\":\(img.height),\"lines\":[\(parts.joined(separator: ","))]}")
}

if pdfMode {
    // usage: lw_ocr --pdf file.pdf  -> one JSON object per page, "file" = "<path>#<page>"
    for f in files {
        guard let doc = PDFDocument(url: URL(fileURLWithPath: f)) else {
            print("{\"file\":\(jsonString(f)),\"error\":\"cannot open pdf\"}"); continue
        }
        for i in 0..<doc.pageCount {
            guard let page = doc.page(at: i) else { continue }
            let box = page.bounds(for: .mediaBox)
            let scale = 2200.0 / max(box.width, box.height)
            let nsimg = page.thumbnail(of: NSSize(width: box.width * scale, height: box.height * scale), for: .mediaBox)
            var rect = NSRect(origin: .zero, size: nsimg.size)
            if let cg = nsimg.cgImage(forProposedRect: &rect, context: nil, hints: nil) {
                recognize(cg, label: "\(f)#\(i + 1)")
            }
        }
    }
} else {
    for f in files {
        let url = URL(fileURLWithPath: f)
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
            print("{\"file\":\(jsonString(f)),\"error\":\"cannot read image\"}")
            continue
        }
        recognize(img, label: f)
    }
}
