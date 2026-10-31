// OCR harness — Apple Vision text recognition for scanned PDFs (architecture.md D2/D3/D15).
//
// One binary, one subprocess call per PDF. Responsibilities:
//   probe per-page signals (embedded-text quality, image coverage, invisible text, codecs)
//   route each page: embedded | ocr | empty   (per-page, architecture.md D9/D15)
//   extract embedded text (PDFKit — handles CID/ToUnicode)
//   OCR scanned pages (3x render, .accurate, en-US, language correction, customWords)
//   LOCAL orientation detection (D22): geometry-gated 4-way A/B — sideways pages are
//     corrected before OCR and before the vision dumps (no LLM involved)
//   positional line assembly + stable line numbers (source-pointer format, D7)
//   --vision-cap N: ALSO writes pageNNN.render-vision.png — the page render
//   downscaled to long edge N for the vision pass only (2026-09-03 D30 cost
//   lever; OCR/grounding keep the full-resolution render)
//   embedded pages now also get REAL per-line geometry (PDFKit characterBounds,
//   2026-09-02 — the verification grounding check needs spans with bboxes on
//   typed pages too; lines whose bounds cannot be resolved keep hasGeometry=false)
//
// Vision-primary (D24, 2026-08-19): every page ALWAYS writes a vision input PNG —
// pageNNN.render-ocr-input.png (the page render; for pure-image pages also the D20
// normalized full-photo render) + pageNNN.image.png (raw embedded photo, D21). The
// text channel (embedded/OCR) feeds ONLY the deterministic verification stack (D30).
//
// Compile on demand: swiftc -O swift/ocr.swift -o swift/.build/ocr   (macOS 15+ for full path)
//
// CLI: ocr process <in.pdf> <outdir> [--scale 3] [--custom-words '["..."]']
//                  [--ocr-only] [--dump-embedded]
// Writes outdir/pageNNN.txt, pageNNN.bbox.txt, report.json + pageNNN.image.png and
// pageNNN.render-ocr-input.png (vision input, unconditional)

import Foundation
import PDFKit
import Vision
import CoreGraphics
import Accelerate

// MARK: - Common types

struct DetectedLine {
    var text: String
    var confidence: Float
    var x0: Float, y0: Float, x1: Float, y1: Float  // normalized, Vision coords (origin bottom-left)
    var hasGeometry: Bool
}

struct PageSignals {
    var chars = 0
    var alphaRatio: Float = 0
    var words = 0
    var imageCoverage: Float = 0
    var invisibleText = false
    var textOps = 0
    var codec: String? = nil
    // True-scan detection: any image XObject with >= 1MP of pixels covering
    // >= 50% of the page. Decorative background images (banners/logos
    // stretched to full-page coverage) never qualify, so a clean digital text
    // layer routes to embedded instead of being re-OCR'd into double-print
    // garbage (D15 note 2026-08-09).
    var hasLargeImage = false
    var topLevelImages = 0
    // Decoded page image XObjects + their CTM (page space). Collected during the
    // probe so OCR can render the scan WITHOUT the parasitic text layer
    // (scan+layer hybrids, D15).
    var imageDraws: [(image: CGImage, transform: CGAffineTransform)] = []
    var imageDecodeFailures = 0
    var imageDims: [[Int]] = []
}

enum PageSource: String {
    case embedded, ocr, empty, failed
}

// MARK: - CGPDF content scanning (signals only, no rendering)

final class ScanState {
    var matrixStack: [CGAffineTransform] = [.identity]
    var current = CGAffineTransform.identity
    var invisibleText = false
    var textOps = 0
    var imageRects: [CGRect] = []
    var pageRect: CGRect = .zero
    var codecs: Set<String> = []
    var parentContent: CGPDFContentStreamRef? = nil
    var table: CGPDFOperatorTableRef? = nil
    var currentXObjects: CGPDFDictionaryRef? = nil
    var formDepth = 0
    var imageDraws: [(image: CGImage, transform: CGAffineTransform)] = []
    var imageDecodeFailures = 0
    var imageDims: [[Int]] = []
    var hasLargeImage = false
    var topLevelImages = 0
}

private func state(_ info: UnsafeMutableRawPointer?) -> ScanState {
    Unmanaged<ScanState>.fromOpaque(info!).takeUnretainedValue()
}

private func scanCm(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    let s = state(info)
    var f: CGFloat = 0, e: CGFloat = 0, d: CGFloat = 0, c: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
    CGPDFScannerPopNumber(scanner, &f)
    CGPDFScannerPopNumber(scanner, &e)
    CGPDFScannerPopNumber(scanner, &d)
    CGPDFScannerPopNumber(scanner, &c)
    CGPDFScannerPopNumber(scanner, &b)
    CGPDFScannerPopNumber(scanner, &a)
    s.current = s.current.concatenating(CGAffineTransform(a: a, b: b, c: c, d: d, tx: e, ty: f))
}

private func scanQ(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    _ = scanner
    state(info).matrixStack.append(state(info).current)
}

private func scanq(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    _ = scanner
    let s = state(info)
    if s.matrixStack.count > 1 { s.matrixStack.removeLast() }
    s.current = s.matrixStack.last ?? .identity
}

private func scanTr(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    let s = state(info)
    var v: Int = 0
    if CGPDFScannerPopInteger(scanner, &v), v == 3 { s.invisibleText = true }
}

private func scanTextOp(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    _ = scanner
    state(info).textOps += 1
}

// Decode a PDF image XObject to CGImage. CGPDFStreamCopyData returns the
// filter-decoded bytes: DCTDecode streams are JPEG bitstreams (ImageIO handles
// them), FlateDecode streams are raw pixels — build the CGImage manually for
// the common DeviceGray/DeviceRGB 8-bit cases. Returns nil when unsupported
// (caller falls back to a full-page render for that page).
private func decodeImageXObject(_ stream: CGPDFStreamRef, _ xdict: CGPDFDictionaryRef) -> CGImage? {
    var format: CGPDFDataFormat = .raw
    guard let data = CGPDFStreamCopyData(stream, &format),
          CFDataGetLength(data) > 0 else { return nil }
    let dataRef = data as CFData

    if let src = CGImageSourceCreateWithData(dataRef, nil),
       let img = CGImageSourceCreateImageAtIndex(src, 0, nil) {
        return img
    }

    var filterPtr: UnsafePointer<Int8>?
    guard CGPDFDictionaryGetName(xdict, "Filter", &filterPtr), let filterPtr else { return nil }
    guard String(cString: filterPtr) == "FlateDecode" else { return nil }

    var w: CGPDFInteger = 0, h: CGPDFInteger = 0, bpc: CGPDFInteger = 0
    CGPDFDictionaryGetInteger(xdict, "Width", &w)
    CGPDFDictionaryGetInteger(xdict, "Height", &h)
    CGPDFDictionaryGetInteger(xdict, "BitsPerComponent", &bpc)
    guard w > 0, h > 0, bpc == 8 else { return nil }

    // D62 (F-R2): resolve the ColorSpace robustly. Scanner output (and
    // CGPDFContext) commonly writes an ARRAY like [/ICCBased <stream>] or an
    // indirect reference — the old name-only lookup failed on both, so the
    // raw image dump and the D20 normalized render silently degraded. The
    // typed dictionary getters follow indirect references; ICCBased exposes
    // the component count via /N (1 = gray, 3 = RGB). The profile itself is
    // irrelevant here (the renders are grayscale).
    var components = 0
    var colorSpace: CGColorSpace?
    var csName: UnsafePointer<Int8>?
    if CGPDFDictionaryGetName(xdict, "ColorSpace", &csName), let csName {
        switch String(cString: csName) {
        case "DeviceGray": components = 1; colorSpace = CGColorSpaceCreateDeviceGray()
        case "DeviceRGB": components = 3; colorSpace = CGColorSpaceCreateDeviceRGB()
        default: break
        }
    }
    if components == 0 {
        var arr: CGPDFArrayRef?
        if CGPDFDictionaryGetArray(xdict, "ColorSpace", &arr), let arr {
            var icc: CGPDFStreamRef?
            if CGPDFArrayGetStream(arr, 1, &icc), let icc,
               let iccDict = CGPDFStreamGetDictionary(icc) {
                var n: CGPDFInteger = 0
                CGPDFDictionaryGetInteger(iccDict, "N", &n)
                if n == 1 { components = 1; colorSpace = CGColorSpaceCreateDeviceGray() }
                else if n == 3 { components = 3; colorSpace = CGColorSpaceCreateDeviceRGB() }
            }
        }
    }
    guard components > 0, let colorSpace else { return nil }
    let alpha: CGImageAlphaInfo = .none
    let rowBytes = Int(w) * components
    guard CFDataGetLength(dataRef) >= rowBytes * Int(h),
          let provider = CGDataProvider(data: dataRef) else { return nil }
    return CGImage(width: Int(w), height: Int(h), bitsPerComponent: 8,
                   bitsPerPixel: 8 * components, bytesPerRow: rowBytes, space: colorSpace,
                   bitmapInfo: CGBitmapInfo(rawValue: alpha.rawValue), provider: provider, decode: nil,
                   shouldInterpolate: false, intent: .defaultIntent)
}

private func scanDo(_ scanner: CGPDFScannerRef, _ info: UnsafeMutableRawPointer?) {
    let s = state(info)
    var namePtr: UnsafePointer<Int8>?
    guard CGPDFScannerPopName(scanner, &namePtr), let namePtr else { return }
    let name = String(cString: namePtr)

    // Resolve the XObject: page level via the content stream (handles inherited resources),
    // form level via the form's own XObject dict (manual walk — GetResource fails on nested streams).
    let xobj: CGPDFObjectRef?
    if let xo = s.currentXObjects {
        var o: CGPDFObjectRef?
        xobj = CGPDFDictionaryGetObject(xo, name, &o) ? o : nil
    } else if let content = s.parentContent {
        xobj = CGPDFContentStreamGetResource(content, "XObject", name)
    } else {
        xobj = nil
    }
    guard let xobj else { return }

    var stream: CGPDFStreamRef?
    guard CGPDFObjectGetValue(xobj, .stream, &stream), let stream,
          let xdict = CGPDFStreamGetDictionary(stream) else { return }

    var subtype: UnsafePointer<Int8>?
    guard CGPDFDictionaryGetName(xdict, "Subtype", &subtype), let subtype else { return }
    let sub = String(cString: subtype)

    if sub == "Image" {
        var filterPtr: UnsafePointer<Int8>?
        if CGPDFDictionaryGetName(xdict, "Filter", &filterPtr), let filterPtr {
            s.codecs.insert(String(cString: filterPtr))
        }
        var w: CGPDFInteger = 0, h: CGPDFInteger = 0
        CGPDFDictionaryGetInteger(xdict, "Width", &w)
        CGPDFDictionaryGetInteger(xdict, "Height", &h)
        guard w > 0, h > 0 else { return }

        let corners = [CGPoint.zero, CGPoint(x: CGFloat(w), y: 0), CGPoint(x: 0, y: CGFloat(h)),
                       CGPoint(x: CGFloat(w), y: CGFloat(h))]
            .map { $0.applying(s.current) }
        let xs = corners.map { $0.x }, ys = corners.map { $0.y }
        let rect = CGRect(x: xs.min()!, y: ys.min()!, width: xs.max()! - xs.min()!, height: ys.max()! - ys.min()!)
        if s.formDepth == 0 { s.topLevelImages += 1 }
        s.imageRects.append(rect)
        let pageArea = max(s.pageRect.width * s.pageRect.height, 1)
        if Int(w) * Int(h) >= 1_000_000, rect.width * rect.height >= 0.5 * pageArea {
            s.hasLargeImage = true
        }
        if let img = decodeImageXObject(stream, xdict) {
            s.imageDraws.append((image: img, transform: s.current))
            s.imageDims.append([img.width, img.height])
        } else {
            s.imageDecodeFailures += 1
        }
    } else if sub == "Form", s.formDepth < 4, let table = s.table {
        // Recurse into the form's content stream, carrying the form's own XObject dict.
        var formXObjects: CGPDFDictionaryRef?
        var formRes: CGPDFDictionaryRef?
        if CGPDFDictionaryGetDictionary(xdict, "Resources", &formRes), let formRes {
            var fxo: CGPDFDictionaryRef?
            if CGPDFDictionaryGetDictionary(formRes, "XObject", &fxo) { formXObjects = fxo }
        }
        let nested = CGPDFContentStreamCreateWithStream(stream, xdict, s.parentContent!)
        let prevX = s.currentXObjects
        let prevCtm = s.current
        s.currentXObjects = formXObjects
        s.formDepth += 1
        let nestedScanner = CGPDFScannerCreate(nested, table, info)
        CGPDFScannerScan(nestedScanner)
        CGPDFScannerRelease(nestedScanner)
        s.formDepth -= 1
        s.currentXObjects = prevX
        s.current = prevCtm
        CGPDFContentStreamRelease(nested)
    }
}

func scanPageContent(_ page: CGPDFPage, pageRect: CGRect) -> PageSignals {
    let state = ScanState()
    state.pageRect = pageRect

    var signals = PageSignals()
    let content = CGPDFContentStreamCreateWithPage(page)
    let table = CGPDFOperatorTableCreate()!
    state.table = table
    state.parentContent = content
    let ctx = Unmanaged.passUnretained(state).toOpaque()
    CGPDFOperatorTableSetCallback(table, "cm") { s, i in scanCm(s, i) }
    CGPDFOperatorTableSetCallback(table, "q") { s, i in scanq(s, i) }
    CGPDFOperatorTableSetCallback(table, "Q") { s, i in scanQ(s, i) }
    CGPDFOperatorTableSetCallback(table, "Tr") { s, i in scanTr(s, i) }
    CGPDFOperatorTableSetCallback(table, "Tj") { s, i in scanTextOp(s, i) }
    CGPDFOperatorTableSetCallback(table, "TJ") { s, i in scanTextOp(s, i) }
    CGPDFOperatorTableSetCallback(table, "'") { s, i in scanTextOp(s, i) }
    CGPDFOperatorTableSetCallback(table, "\"") { s, i in scanTextOp(s, i) }
    CGPDFOperatorTableSetCallback(table, "Do") { s, i in scanDo(s, i) }

    let scanner = CGPDFScannerCreate(content, table, ctx)
    CGPDFScannerScan(scanner)
    signals.invisibleText = state.invisibleText
    signals.textOps = state.textOps
    signals.codec = state.codecs.contains("CCITTFaxDecode") ? "CCITT" : (state.codecs.contains("JBIG2Decode") ? "JBIG2" : state.codecs.first)
    signals.imageDraws = state.imageDraws
    signals.imageDecodeFailures = state.imageDecodeFailures
    signals.imageDims = state.imageDims
    signals.hasLargeImage = state.hasLargeImage
    signals.topLevelImages = state.topLevelImages
    let pageArea = max(pageRect.width * pageRect.height, 1)
    signals.imageCoverage = Float(min(state.imageRects.reduce(CGFloat(0)) { $0 + $1.width * $1.height } / pageArea, 1.0))
    CGPDFScannerRelease(scanner)
    CGPDFOperatorTableRelease(table)
    CGPDFContentStreamRelease(content)
    return signals
}

// MARK: - Rendering (PDFKit, 3x, grayscale)

// D20 (2026-08-10): honor /Rotate + native-resolution scale. PDFKit's
// draw(with: .mediaBox) does NOT apply the page rotation, so the canvas is
// sized in the DISPLAYED (rotated) dimensions and the rotation transform is
// applied ourselves — matching what PDF viewers show (some phone-photo PDFs
// carry /Rotate 270).
func renderPage(_ page: PDFPage, scale: CGFloat, maxDimension: CGFloat = 4000) -> CGImage? {
    let bounds = page.bounds(for: .mediaBox)
    let rotation = page.rotation % 360
    let rotated = (rotation == 90 || rotation == 270)
    let cw = rotated ? bounds.height : bounds.width
    let ch = rotated ? bounds.width : bounds.height
    let raw = max(cw, ch) * scale
    let effScale = raw > maxDimension ? maxDimension / max(cw, ch) : scale
    let w = Int(ceil(cw * effScale)), h = Int(ceil(ch * effScale))
    guard w > 0, h > 0,
          let cs = CGColorSpace(name: CGColorSpace.linearGray),
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: cs, bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: effScale, y: effScale)
    if rotated {
        // Empirically calibrated against the viewer-rendered screenshot
        // (2026-08-10): /Rotate 90 -> 90 deg counterclockwise; /Rotate 270 ->
        // 90 deg clockwise. The initial patch had these swapped, producing an
        // upside-down document (OCR read the footer first).
        switch rotation {
        case 90:
            ctx.translateBy(x: 0, y: ch)
            ctx.rotate(by: -.pi / 2)
        case 270:
            ctx.translateBy(x: cw, y: 0)
            ctx.rotate(by: .pi / 2)
        default: break
        }
    }
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

// Native-resolution scale: dominant image's intrinsic pixels per displayed point.
func nativeScale(_ page: PDFPage, draws: [(image: CGImage, transform: CGAffineTransform)]) -> CGFloat {
    // The content stream's cm scales the image's UNIT square to the page rect
    // (image pixels -> /W,/H -> unit square -> cm -> points). So the placed
    // width in points = |transform| applied to the unit square; the intrinsic
    // pixels-per-point ratio = image dims / placed dims.
    guard !draws.isEmpty else { return 0 }
    var best: CGFloat = 0
    for (img, t) in draws {
        let wpts = hypot(t.a, t.c)
        let hpts = hypot(t.b, t.d)
        if wpts > 1, hpts > 1 {
            best = max(best, CGFloat(img.width) / wpts, CGFloat(img.height) / hpts)
        }
    }
    return best
}

// D21 (2026-08-10): raw embedded-image extraction for the vision pass.
// A pure-image page (single top-level phone photo placed at 1px=1pt) is the
// document itself — but ANY page-bounds render
// (PDFKit renderPage, Poppler, even the provider's own PDF rasterizer, probe
// 2026-08-10) clips the photo to the page box, cutting off the letterhead
// (Luna then reads the shipping-bill block instead of the tax invoice). The
// robust path: take the DECODED image XObject (full intrinsic resolution,
// color — the renders are grayscale) and apply the page /Rotate ourselves.
// Rotation branches calibrated the same way as renderPage's (270° corpus-
// proven; 90°/180° written to the same convention).
func rotateImage(_ img: CGImage, rotation: Int) -> CGImage? {
    let r = rotation % 360
    let rotated = (r == 90 || r == 270)
    let cw = rotated ? CGFloat(img.height) : CGFloat(img.width)
    let ch = rotated ? CGFloat(img.width) : CGFloat(img.height)
    let w = Int(ceil(cw)), h = Int(ceil(ch))
    guard w > 0, h > 0,
          let cs = CGColorSpace(name: CGColorSpace.sRGB),
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                              bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
    else { return nil }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    switch r {
    case 90:
        ctx.translateBy(x: 0, y: ch)
        ctx.rotate(by: -.pi / 2)
    case 180:
        ctx.translateBy(x: cw, y: ch)
        ctx.rotate(by: .pi)
    case 270:
        ctx.translateBy(x: cw, y: 0)
        ctx.rotate(by: .pi / 2)
    default:
        break
    }
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: CGFloat(img.width), height: CGFloat(img.height)))
    return ctx.makeImage()
}

// D22 (2026-08-10): LOCAL page-orientation detection — Apple Vision geometry
// only, no LLM. A page whose print is rotated ~90° (seen in scanned invoices)
// yields text-line boxes that are TALLER than wide; clean pages yield wide
// boxes. The gate = the tall-aspect line fraction. When it fires, OCR runs on
// ALL FOUR orientations (0/90/180/270) of the render and the highest-scoring
// composite wins (wide-line fraction, line count, mean confidence).
//
// D61 (2026-09-29) — the audit proved the 4-way A/B cannot be trusted:
//  - the GATE runs on RAW fragments, not assembled lines: sideways columns
//    share y-centers and merge into one WIDE union, hiding the tall-line
//    signal (the "merged sideways columns" fixture never fired the gate);
//  - the composite CANNOT tell upright from 180°-flipped: Apple Vision reads
//    upside-down text just as confidently (measured: exact score ties 3.15 vs
//    3.15; the same page flips its winning delta between identical runs; the
//    score gap is OCR noise, not signal), and no page-content prior separates
//    the two (text mass and largest-line position measured on the corpus: no
//    separation). File-level consensus amplifies the noise, it does not fix it;
//  - therefore pages are NO LONGER rotated. A sideways print is DETECTED and
//    reported (`orientation: unresolved`) and the page keeps its own frame —
//    the VLM render, the OCR spans and the vision dumps all share that frame,
//    so grounding stays consistent and no page is ever flipped upside-down.
//  - pure-180° pages never fire the gate (wide lines) and stay a documented
//    limitation; the VLM reads them (same frame) but a reviewer sees them
//    rotated. A semantic orientation classifier is future work.
// All local, zero LLM calls.
let ORIENTATION_GATE: Float = 0.35   // tall-line fraction that marks a page sideways

func orientationScore(_ lines: [DetectedLine]) -> Float {
    var tall: Float = 0
    var total: Float = 0
    for l in lines where l.hasGeometry {
        let w = l.x1 - l.x0
        let h = l.y1 - l.y0
        if w > 0, h > 0 {
            total += 1
            if h > w { tall += 1 }
        }
    }
    return total > 0 ? tall / total : 0
}

// Composite readability score for one OCR pass: wide-line fraction (0-1,
// doubled — horizontal text is the strongest signal), line count (0-1,
// normalized at 50 lines), mean confidence (0-1). Range ~0-4; a clean
// upright invoice scores ~3.5-4, a sideways read ~2-2.5.
func compositeScore(_ lines: [DetectedLine]) -> Float {
    var wide: Float = 0
    var total: Float = 0
    for l in lines where l.hasGeometry {
        let w = l.x1 - l.x0
        let h = l.y1 - l.y0
        if w > 0, h > 0 {
            total += 1
            if w >= h { wide += 1 }
        }
    }
    let wideFrac = total > 0 ? wide / total : 0
    let meanConf = lines.isEmpty ? 0 : lines.reduce(0) { $0 + $1.confidence } / Float(lines.count)
    return wideFrac * 2.0 + min(Float(lines.count) / 50.0, 1.0) + meanConf
}

// MARK: - Image statistics (D16 quality gate: blank-with-image detection)

// Computed on the existing 3x render. Signals:
//   std_dev            grayscale standard deviation (0 = flat/blank image)
//   dark_ratio         fraction of pixels below 100/255 (ink coverage)
//   laplacian_variance variance of a 3x3 Laplacian convolution — a sharpness
//                      metric: blank scans and out-of-focus pages are ~0.
// A page with image content (std_dev or dark_ratio high) that OCR failed to
// read is "poor" (unreadable), not "blank" — the Python gate decides using
// these numbers (config/thresholds.yaml quality:).
func imageStats(_ image: CGImage) -> [String: Double]? {
    guard let provider = image.dataProvider,
          let data = provider.data,
          let ptr = CFDataGetBytePtr(data) else { return nil }
    let w = image.width, h = image.height
    guard w > 0, h > 0 else { return nil }
    let bpr = image.bytesPerRow
    let n = w * h

    // Contiguous grayscale pixels (bpr may include row padding).
    var pixels = [UInt8](repeating: 0, count: n)
    for y in 0..<h { memcpy(&pixels[y * w], ptr + y * bpr, w) }

    // D63: accumulate in DOUBLE. Float32 sums over multi-megapixel images
    // lose precision (a uniform 253 render reported std_dev ~16), which
    // misclassified blank pages as "poor" (and D62 sends poor pages to the
    // vision model).
    var doubles = [Double](repeating: 0, count: n)
    vDSP_vfltu8D(pixels, 1, &doubles, 1, vDSP_Length(n))
    var sum = 0.0, sumSq = 0.0
    vDSP_sveD(doubles, 1, &sum, vDSP_Length(n))
    vDSP_svesqD(doubles, 1, &sumSq, vDSP_Length(n))
    let count = Double(n)
    let mean = sum / count
    let stdDev = sqrt(max(0, sumSq / count - mean * mean))

    var darkCount = 0
    for v in pixels where v < 100 { darkCount += 1 }

    // Laplacian variance: convolve with [[0,1,0],[1,-4,1],[0,1,0]] (divisor 1,
    // bias 128 so negative responses are representable in Planar8), then the
    // variance of the result ≈ variance of the Laplacian (kernel is DC-free).
    var lapVariance: Double = 0
    let kernel: [Int16] = [0, 1, 0, 1, -4, 1, 0, 1, 0]
    var src = vImage_Buffer()
    var dest = vImage_Buffer()
    var destBytes = [UInt8](repeating: 0, count: n)
    let err = pixels.withUnsafeBytes { (px: UnsafeRawBufferPointer) -> vImage_Error in
        src.data = UnsafeMutableRawPointer(mutating: px.baseAddress!)
        src.height = vImagePixelCount(h)
        src.width = vImagePixelCount(w)
        src.rowBytes = w
        return destBytes.withUnsafeMutableBytes { (db: UnsafeMutableRawBufferPointer) -> vImage_Error in
            dest.data = db.baseAddress
            dest.height = vImagePixelCount(h)
            dest.width = vImagePixelCount(w)
            dest.rowBytes = w
            return vImageConvolve_Planar8(&src, &dest, nil, 0, 0, kernel, 3, 3, 1, 128,
                                          vImage_Flags(kvImageEdgeExtend))
        }
    }
    if err == kvImageNoError {
        var lapDoubles = [Double](repeating: 0, count: n)
        vDSP_vfltu8D(destBytes, 1, &lapDoubles, 1, vDSP_Length(n))
        var lapSum = 0.0, lapSumSq = 0.0
        vDSP_sveD(lapDoubles, 1, &lapSum, vDSP_Length(n))
        vDSP_svesqD(lapDoubles, 1, &lapSumSq, vDSP_Length(n))
        let lapMean = lapSum / count
        let lapStd = sqrt(max(0, lapSumSq / count - lapMean * lapMean))
        lapVariance = lapStd * lapStd
    }
    return [
        "std_dev": Double(stdDev),
        "dark_ratio": Double(darkCount) / Double(n),
        "laplacian_variance": lapVariance,
    ]
}

// MARK: - OCR

func ocrImage(_ image: CGImage, customWords: [String]) throws -> [DetectedLine] {
    if #available(macOS 15.0, *) {
        var request = RecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.recognitionLanguages = [Locale.Language(identifier: "en-US")]
        request.usesLanguageCorrection = true
        if !customWords.isEmpty { request.customWords = customWords }

        let semaphore = DispatchSemaphore(value: 0)
        var observations: [RecognizedTextObservation] = []
        var performError: Error?
        Task {
            do { observations = try await request.perform(on: image) }
            catch { performError = error }
            semaphore.signal()
        }
        semaphore.wait()
        if let performError { throw performError }
        return observations.map { obs in
            let bb = obs.boundingBox
            let c = obs.topCandidates(1).first
            return DetectedLine(text: c?.string ?? "", confidence: c?.confidence ?? 0,
                                x0: Float(bb.origin.x), y0: Float(bb.origin.y),
                                x1: Float(bb.origin.x + bb.width), y1: Float(bb.origin.y + bb.height),
                                hasGeometry: true)
        }
    } else {
        let request = VNRecognizeTextRequest()
        request.revision = VNRecognizeTextRequestRevision3
        request.recognitionLevel = .accurate
        request.recognitionLanguages = ["en-US"]
        request.usesLanguageCorrection = true
        if !customWords.isEmpty { request.customWords = customWords }
        try VNImageRequestHandler(cgImage: image).perform([request])
        return (request.results ?? []).map { obs in
            let c = obs.topCandidates(1).first
            return DetectedLine(text: c?.string ?? "", confidence: c?.confidence ?? 0,
                                x0: Float(obs.boundingBox.minX), y0: Float(obs.boundingBox.minY),
                                x1: Float(obs.boundingBox.maxX), y1: Float(obs.boundingBox.maxY),
                                hasGeometry: true)
        }
    }
}

// MARK: - Line assembly (reading order + positional spacing)

// A merged line plus the fragment indices it was built from (1c pre-step:
// pageNNN.segments.txt needs the fragment-level geometry).
struct AssembledLines {
    var lines: [DetectedLine]
    var groups: [[Int]]
}

func assembleLinesWithGroups(_ lines: [DetectedLine]) -> AssembledLines {
    guard !lines.isEmpty else { return AssembledLines(lines: [], groups: []) }
    let heights = lines.map { $0.y1 - $0.y0 }.sorted()
    let medianHeight = heights[heights.count / 2]
    let tolerance = max(0.002, Float(medianHeight) * 0.5)

    let sorted = lines.enumerated().sorted { a, b in
        let ay = (a.element.y0 + a.element.y1) / 2, by = (b.element.y0 + b.element.y1) / 2
        if abs(ay - by) > tolerance { return ay > by }  // top-to-bottom (Vision y-up)
        return a.element.x0 < b.element.x0              // left-to-right within a line
    }

    var result: [DetectedLine] = []
    var groups: [[Int]] = []
    for (idx, line) in sorted {
        let centerY = (line.y0 + line.y1) / 2
        if let last = result.last, abs((last.y0 + last.y1) / 2 - centerY) <= tolerance {
            // Merge into the current line: order by x, join with spacing proportional to the x-gap.
            let gap = Float(line.x0) - Float(last.x1)
            let sep = gap > 0.03 ? "    " : (gap > 0.005 ? " " : "")
            result[result.count - 1] = DetectedLine(
                text: last.text + sep + line.text,
                confidence: min(last.confidence, line.confidence),
                x0: last.x0, y0: min(last.y0, line.y0), x1: line.x1, y1: max(last.y1, line.y1),
                hasGeometry: true)
            groups[groups.count - 1].append(idx)
        } else {
            result.append(line)
            groups.append([idx])
        }
    }
    return AssembledLines(lines: result, groups: groups)
}

// MARK: - Embedded text (PDFKit)

func embeddedText(_ page: PDFPage) -> [String] {
    (page.string ?? "")
        .components(separatedBy: .newlines)
        .map { $0.trimmingCharacters(in: .whitespaces) }
        .filter { !$0.isEmpty }
}

// Per-line geometry for embedded pages (2026-09-02, verification grounding;
// selection-based since 2026-09-22). Boxes are PDF user space, origin
// bottom-left — the same orientation convention as the Vision OCR bbox rows.
// Returns one (text, box?) per OUTPUT line, identical text/filtering to
// embeddedText() so pageNNN.txt stays byte-identical.
//
// PRIMARY: `page.selection(for:)` — PDFSelection shares page.string's index
// space and returns the line's true box. `characterBounds(at:)` does NOT for
// multi-column embedded invoices: its index space diverges from page.string
// and the union drifts by ~a line (measured on the corpus: a reference-number
// line's union spanned both columns and two rows; a GSTIN line unioned to
// x 0.25-0.87). characterBounds stays as the fallback.
func embeddedLinesGeometry(_ page: PDFPage) -> [(text: String, box: CGRect?)] {
    let comps = (page.string ?? "")
        .components(separatedBy: .newlines)
    guard !comps.isEmpty else { return [] }
    var out: [(String, CGRect?)] = []
    var idx = 0
    for piece in comps {
        let trimmed = piece.trimmingCharacters(in: .whitespaces)
        var box: CGRect?
        if !piece.isEmpty {
            if let sel = page.selection(for: NSRange(location: idx, length: piece.count)) {
                let b = sel.bounds(for: page)
                if !b.isNull && !b.isEmpty { box = b }
            }
            if box == nil {
                for offset in 0..<piece.count {
                    let b = page.characterBounds(at: idx + offset)
                    if b.isNull || b.isEmpty { continue }
                    box = box?.union(b) ?? b
                }
            }
        }
        idx += piece.count + 1  // +1 for the consumed newline separator
        if !trimmed.isEmpty {
            out.append((trimmed, box))
        }
    }
    return out
}

// MARK: - Classification

func classify(_ signals: PageSignals) -> PageSource {
    // Clean embedded text -> use the text layer, never re-OCR it. Amended
    // 2026-08-09: the old `imageCoverage < 0.8` gate (and the later
    // hasLargeImage gate) misrouted digital pages with decorative background
    // images (banners/logos stretched to full-page coverage, typed pages with
    // decorative images). Their text layers are the AUTHORITATIVE
    // content: PDFKit extracts them byte-clean vs ground truth, while
    // re-OCR'ing the rendered page fuses the layer over the background into
    // double-print garbage (the documented "~48% WER layer" was a
    // mis-measurement — see D15 note). A genuinely bad layer fails the
    // alpha/words floors below and still routes to OCR.
    if signals.chars >= 100, !signals.invisibleText,
       signals.alphaRatio >= 0.5 || signals.words >= 20 {
        return .embedded
    }
    if signals.chars == 0, signals.textOps == 0, signals.imageCoverage == 0 {
        return .empty
    }
    return .ocr  // scanned (no layer), or a layer that failed the quality floors — OCR (D15)
}

func textSignals(_ text: String) -> (chars: Int, alpha: Float, words: Int) {
    let chars = text.count
    let alphas = text.filter { $0.isLetter }.count
    let words = text.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).count
    return (chars, chars > 0 ? Float(alphas) / Float(chars) : 0, words)
}

// MARK: - Output helpers

func writeReport(path: String, report: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) else { return }
    try? data.write(to: URL(fileURLWithPath: path))
}

// D20 (2026-08-10): normalized image placement renderer. Some phone-photo
// PDFs place their JPEG at 1px = 1pt (image user-size = pixel size), so
// PDFKit's page.draw shows only the mediaBox sliver (bottom-left ~842x595px) —
// cropping the letterhead + invoice number. PDF viewers (PDF.js) scale the
// image to the box (unit-square semantics), showing the full document. This
// renderer applies that normalization: draw the image into the UNIT rect under
// the ORIGINAL cm transform -> full image fills the box. Only used for
// pure-image pages (textOps == 0, image coverage >= 0.95).
func renderPageImagesNormalized(_ page: PDFPage,
                                draws: [(image: CGImage, transform: CGAffineTransform)],
                                scale: CGFloat, maxDimension: CGFloat = 4000) -> CGImage? {
    guard !draws.isEmpty else { return nil }
    let rotation = page.rotation % 360
    let rotated = (rotation == 90 || rotation == 270)
    let bounds = page.bounds(for: .mediaBox)
    let cw = rotated ? bounds.height : bounds.width
    let ch = rotated ? bounds.width : bounds.height
    let raw = max(cw, ch) * scale
    let effScale = raw > maxDimension ? maxDimension / max(cw, ch) : scale
    let w = Int(ceil(cw * effScale)), h = Int(ceil(ch * effScale))
    guard w > 0, h > 0,
          let cs = CGColorSpace(name: CGColorSpace.linearGray),
          let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: cs, bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: effScale, y: effScale)
    if rotated {
        switch rotation {
        case 90:
            ctx.translateBy(x: 0, y: ch)
            ctx.rotate(by: -.pi / 2)
        case 270:
            ctx.translateBy(x: cw, y: 0)
            ctx.rotate(by: .pi / 2)
        default: break
        }
    }
    // Pixel-proven (2026-08-10): PDFKit's page.draw places the image at
    // 1px = 1pt (strict spec), so the page box shows only the photo's
    // bottom-left 842x595-px region (MAD 9.4 vs the cropped JPEG) — the
    // letterhead + invoice number are cropped out. Viewers scale the image
    // into the box (unit-square semantics). Here: draw the image into the
    // UNIT rect under the ORIGINAL cm transform -> full image fills the box.
    for (img, t) in draws {
        ctx.saveGState()
        ctx.concatenate(t)
        ctx.interpolationQuality = CGInterpolationQuality.high
        ctx.draw(img, in: CGRect(x: 0, y: 0, width: 1, height: 1))
        ctx.restoreGState()
    }
    return ctx.makeImage()
}

// Encode a render to PNG bytes (vision input dumps, D21/D24).
func CGImageDestinationPNG(_ image: CGImage) -> Data? {
    let out = NSMutableData()
    guard let dest = CGImageDestinationCreateWithData(out, "public.png" as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(dest, image, nil)
    guard     CGImageDestinationFinalize(dest) else { return nil }
    return out as Data
}

// D30 cost lever (2026-09-03): the vision pass's input is dimension-tiled by
// the model provider, so a downscaled COPY is far cheaper per page (roughly
// half the input tokens per halved long edge) while OCR/grounding keep the
// full-resolution render. Written as pageNNN.render-vision.png only when
// `--vision-cap <longEdge>` is given; extract.py prefers it when present.
// Draw into a fresh context with the same grayscale space as the renders.
func downscaledTo(_ image: CGImage, longEdge: Int) -> CGImage? {
    guard longEdge > 0 else { return nil }
    let w = image.width, h = image.height
    let long = max(w, h)
    guard long > longEdge else { return image }
    let scale = CGFloat(longEdge) / CGFloat(long)
    let tw = max(Int(round(CGFloat(w) * scale)), 1)
    let th = max(Int(round(CGFloat(h) * scale)), 1)
    guard let cs = CGColorSpace(name: CGColorSpace.linearGray),
          let ctx = CGContext(data: nil, width: tw, height: th, bitsPerComponent: 8,
                              bytesPerRow: 0, space: cs,
                              bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return nil }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: tw, height: th))
    ctx.interpolationQuality = .high
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: tw, height: th))
    return ctx.makeImage()
}

// Write pageNNN.render-vision.png when the capture was made with
// `--vision-cap` (the A/B'd model-input downscale, D30).
func writeVisionRender(_ image: CGImage, outDir: URL, pageNo: Int, cap: Int) {
    // PNG on purpose (D42 measurement 2026-09-25): JPEG q85 was LARGER than
    // lossless PNG for these text-on-white renders (measured on the corpus:
    // ~366KB vs ~203KB; ~3.2MB vs ~2.7MB) — flat areas + crisp glyph edges
    // favour PNG.
    guard cap > 0, let down = downscaledTo(image, longEdge: cap),
          let png = CGImageDestinationPNG(down) else { return }
    try? png.write(to: outDir.appendingPathComponent(
        String(format: "page%03d.render-vision.png", pageNo)))
}

// MARK: - Main

func usage() -> Never {
    fputs("""
    usage: ocr process <in.pdf> <outdir> [--scale 3] [--custom-words '["..."]']
                     [--ocr-only] [--dump-embedded] [--vision-cap N]
      process  per-page routing (embedded vs OCR), writes pageNNN.txt + pageNNN.bbox.txt + report.json
      --ocr-only     force OCR on the selected pages (skips embedded-text route)
      --dump-embedded  also write pageNNN.embedded.txt = PDFKit extraction of the text
                     layer for EVERY page (debug/A-B; OCR pages would otherwise discard it)
      --vision-cap N   also write pageNNN.render-vision.png downscaled to N px on the long
                     edge (the vision pass reads it; OCR/grounding keep the full render; 0 = off)
      (vision input PNGs — pageNNN.render-ocr-input.png + pageNNN.image.png — are ALWAYS written,
       D24 vision-primary: every page must have a render for the vision extraction pass)
    """, stderr)
    exit(64)
}

func segmentsFor(_ assembled: AssembledLines, _ fragments: [DetectedLine]) -> [String] {
    var segments: [String] = []
    for (lineIdx, group) in assembled.groups.enumerated() {
        for fragIdx in group {
            let fr = fragments[fragIdx]
            segments.append(String(
                format: "%d|%.4f,%.4f,%.4f,%.4f|%.2f|%@",
                lineIdx + 1, fr.x0, fr.y0, fr.x1, fr.y1, fr.confidence, fr.text))
        }
    }
    return segments
}

// Per-page outputs for OCR pages: text + bbox + segments + image stats. The
// vision dumps were written before OCR and stay as-is (D61: the page frame is
// never rotated, so the dumps and the OCR channel always share one frame).
func writeOCRPage(outURL: URL, pageNo: Int, lines: [DetectedLine], segments: [String],
                  image: CGImage, pageReport: inout [String: Any]) {
    let linesPath = outURL.appendingPathComponent(String(format: "page%03d.txt", pageNo))
    try? lines.map(\.text).joined(separator: "\n").write(to: linesPath, atomically: true, encoding: .utf8)
    let bboxPath = outURL.appendingPathComponent(String(format: "page%03d.bbox.txt", pageNo))
    let bboxContent = lines.enumerated().map { idx, line in
        let geo = line.hasGeometry
            ? String(format: "%.4f,%.4f,%.4f,%.4f", line.x0, line.y0, line.x1, line.y1)
            : ""
        return "\(idx + 1)|\(geo)|\(String(format: "%.2f", line.confidence))|\(line.text)"
    }.joined(separator: "\n")
    try? bboxContent.write(to: bboxPath, atomically: true, encoding: .utf8)
    if !segments.isEmpty {
        try? segments.joined(separator: "\n").write(
            to: outURL.appendingPathComponent(String(format: "page%03d.segments.txt", pageNo)),
            atomically: true, encoding: .utf8)
    }
    pageReport["image_stats"] = imageStats(image) ?? [:]
}

func main() {
    var args = CommandLine.arguments
    guard args.count >= 2, args[1] == "process" else { usage() }
    args.removeFirst(2)
    guard let inPath = args.first else { usage() }
    args.removeFirst()
    guard let outDir = args.first else { usage() }
    args.removeFirst()

    var scale: CGFloat = 3
    var customWords: [String] = []
    var ocrOnly = false
    var dumpEmbedded = false
    var visionCap = 0
    var i = 0
    while i < args.count {
        switch args[i] {
        case "--scale": i += 1; scale = CGFloat(Double(args[i]) ?? 3)
        case "--custom-words": i += 1
            customWords = (try? JSONSerialization.jsonObject(with: Data(args[i].utf8)) as? [String]) ?? []
        case "--ocr-only": ocrOnly = true
        case "--dump-embedded": dumpEmbedded = true
        case "--vision-cap": i += 1; visionCap = Int(args[i]) ?? 0
        default: fputs("unknown arg: \(args[i])\n", stderr); exit(64)
        }
        i += 1
    }

    let inURL = URL(fileURLWithPath: inPath)
    let outURL = URL(fileURLWithPath: outDir)
    try? FileManager.default.createDirectory(at: outURL, withIntermediateDirectories: true)

    var fileReport: [String: Any] = [
        "file": inURL.lastPathComponent,
        "mode": ocrOnly ? "ocr-only" : "auto",
    ]

    guard let doc = PDFDocument(url: inURL) else {
        fileReport["error"] = "cannot open PDF (corrupt or unsupported)"
        fileReport["status"] = "failed"
        writeReport(path: (outURL.appendingPathComponent("report.json")).path, report: fileReport)
        exit(2)
    }

    var pageReports: [[String: Any]] = []
    var totalOCRms: Double = 0
    var anyFailure = false

    for pageIndex in 0..<doc.pageCount {
        let pageNo = pageIndex + 1
        guard let page = doc.page(at: pageIndex) else {
            pageReports.append(["page": pageNo, "source": "failed", "error": "no page object"])
            anyFailure = true
            continue
        }

        var pageReport: [String: Any] = ["page": pageNo]
        var pdfSignals = scanPageContent(page.pageRef!, pageRect: page.bounds(for: .mediaBox))

        let embeddedLines: [String]
        if ocrOnly {
            embeddedLines = []
        } else {
            embeddedLines = embeddedText(page)
            let t = textSignals(embeddedLines.joined(separator: "\n"))
            pdfSignals.chars = t.chars
            pdfSignals.alphaRatio = t.alpha
            pdfSignals.words = t.words
        }
        if dumpEmbedded {
            let embeddedDump = embeddedLines.joined(separator: "\n")
            try? embeddedDump.write(
                to: outURL.appendingPathComponent(String(format: "page%03d.embedded.txt", pageNo)),
                atomically: true, encoding: .utf8)
        }
        pageReport["signals"] = [
            "chars": pdfSignals.chars, "alpha": pdfSignals.alphaRatio, "words": pdfSignals.words,
            "image_coverage": pdfSignals.imageCoverage, "invisible_text": pdfSignals.invisibleText,
            "text_ops": pdfSignals.textOps, "codec": pdfSignals.codec ?? "",
            "images": pdfSignals.imageDraws.count, "image_decode_failures": pdfSignals.imageDecodeFailures,
            "image_dims": pdfSignals.imageDims, "has_large_image": pdfSignals.hasLargeImage,
            "top_level_images": pdfSignals.topLevelImages,
        ]

        let source: PageSource = ocrOnly ? .ocr : classify(pdfSignals)
        pageReport["source"] = source.rawValue

        var lines: [DetectedLine] = []
        switch source {
        case .embedded:
            var y: Float = 1.0
            var geoCount = 0
            let pb = max(page.bounds(for: .mediaBox).width, 1)
            let ph = max(page.bounds(for: .mediaBox).height, 1)
            for (text, box) in embeddedLinesGeometry(page) {
                if let box {
                    lines.append(DetectedLine(text: text, confidence: 1.0,
                                              x0: Float(box.minX / pb), y0: Float(box.minY / ph),
                                              x1: Float(box.maxX / pb), y1: Float(box.maxY / ph),
                                              hasGeometry: true))
                    geoCount += 1
                } else {
                    lines.append(DetectedLine(text: text, confidence: 1.0,
                                              x0: 0, y0: y, x1: 0, y1: y, hasGeometry: false))
                }
                y -= 0.001
            }
            pageReport["embedded_geometry"] = lines.isEmpty
                ? "none" : (geoCount == lines.count ? "true" : "partial")
            // Vision-primary (D24): embedded pages also get a vision input —
            // the PDFKit page render (digital pages are vector; a viewer-equivalent
            // render is the right image for the vision extraction pass).
            if let image = renderPage(page, scale: scale),
               let png = CGImageDestinationPNG(image) {
                try? png.write(to: outURL.appendingPathComponent(String(format: "page%03d.render-ocr-input.png", pageNo)))
                writeVisionRender(image, outDir: outURL, pageNo: pageNo, cap: visionCap)
                pageReport["render"] = "page"
            } else {
                pageReport["render"] = "none"
            }
        case .ocr:
            let started = DispatchTime.now()
            var image: CGImage?
            var renderMode = "page"
            let pureImagePage = pdfSignals.textOps == 0
                && pdfSignals.imageCoverage >= 0.95
                && pdfSignals.imageDraws.count == 1   // single top-level image only
                && pdfSignals.imageDecodeFailures == 0
                && pdfSignals.topLevelImages == 1
            let native = nativeScale(page, draws: pdfSignals.imageDraws)
            let renderScale = pureImagePage ? max(scale, native) : scale
            if pureImagePage {
                image = renderPageImagesNormalized(page, draws: pdfSignals.imageDraws, scale: renderScale)
                renderMode = "normalized-images"
                pageReport["render_note"] = "image placement normalized (1px=1pt PDF fix)"
            } else {
                image = renderPage(page, scale: renderScale)
            }
            pageReport["render"] = renderMode
            if let image {
                // Vision input dumps (unconditional — D24 vision-primary):
                // the OCR input — what the harness actually reads (for
                // pure-image pages: the D20 normalized full-photo render;
                // otherwise the page render). Named for what it IS, so
                // vision/debug code never mistakes it for a page-bounds
                // render (D21.1, 2026-08-10).
                if let png = CGImageDestinationPNG(image) {
                    try? png.write(to: outURL.appendingPathComponent(String(format: "page%03d.render-ocr-input.png", pageNo)))
                }
                writeVisionRender(image, outDir: outURL, pageNo: pageNo, cap: visionCap)
                // D21: raw embedded-image extraction — the COMPLETE photo
                // (color, native resolution, /Rotate applied). Page-bounds
                // renders clip 1px=1pt photos; this file cannot.
                if pureImagePage,
                   let raw = pdfSignals.imageDraws.first?.image,
                   let oriented = rotateImage(raw, rotation: page.rotation),
                   let png = CGImageDestinationPNG(oriented) {
                    try? png.write(to: outURL.appendingPathComponent(String(format: "page%03d.image.png", pageNo)))
                }
                do {
                    let fragments = try ocrImage(image, customWords: customWords)
                    let assembled = assembleLinesWithGroups(fragments)
                    lines = assembled.lines
                    // D61: detection on RAW fragments (merged sideways columns
                    // collapse into one wide union and hide the tall-line
                    // signal — D22/D61 note above). A detected sideways print
                    // is reported but NOT rotated: the page keeps its own
                    // frame (no 180° coin flip; see the D61 note).
                    let s0 = orientationScore(fragments)
                    let sideways = s0 > ORIENTATION_GATE
                    pageReport["orientation_tall_fraction"] = Double(s0)
                    pageReport["orientation"] = sideways ? "unresolved" : "upright"
                    pageReport["orientation_confidence"] = sideways
                        ? "sideways-kept-original" : "upright"
                    if sideways {
                        pageReport["render_note"] = "orientation unresolved: sideways print detected — kept original frame (D61)"
                    }
                    writeOCRPage(outURL: outURL, pageNo: pageNo,
                                 lines: assembled.lines,
                                 segments: segmentsFor(assembled, fragments),
                                 image: image, pageReport: &pageReport)
                    pageReport["lines"] = assembled.lines.count
                    pageReport["words"] = assembled.lines.reduce(0) {
                        $0 + $1.text.split(whereSeparator: { $0.isWhitespace }).count
                    }
                } catch {
                    pageReport["error"] = "\(error)"
                    pageReport["source"] = "failed"
                    anyFailure = true
                    writeOCRPage(outURL: outURL, pageNo: pageNo, lines: [],
                                 segments: [], image: image,
                                 pageReport: &pageReport)
                    pageReport["lines"] = 0
                    pageReport["words"] = 0
                }
            } else {
                pageReport["error"] = "render failed"
                pageReport["source"] = "failed"
                anyFailure = true
                let linesPath = outURL.appendingPathComponent(String(format: "page%03d.txt", pageNo))
                try? "".write(to: linesPath, atomically: true, encoding: .utf8)
                let bboxPath = outURL.appendingPathComponent(String(format: "page%03d.bbox.txt", pageNo))
                try? "".write(to: bboxPath, atomically: true, encoding: .utf8)
                pageReport["lines"] = 0
                pageReport["words"] = 0
            }
            let elapsed = Double(DispatchTime.now().uptimeNanoseconds - started.uptimeNanoseconds) / 1_000_000
            totalOCRms += elapsed
            pageReport["ocr_ms"] = Int(elapsed)
            pageReports.append(pageReport)
        case .empty:
            break
        case .failed:
            break
        }

        // Write page files with stable line numbers (non-OCR pages; OCR pages
        // are written by writeOCRPage).
        if source != .ocr {
            let linesPath = outURL.appendingPathComponent(String(format: "page%03d.txt", pageNo))
            try? lines.map(\.text).joined(separator: "\n").write(to: linesPath, atomically: true, encoding: .utf8)
            let bboxPath = outURL.appendingPathComponent(String(format: "page%03d.bbox.txt", pageNo))
            let bboxContent = lines.enumerated().map { idx, line in
                let geo = line.hasGeometry
                    ? String(format: "%.4f,%.4f,%.4f,%.4f", line.x0, line.y0, line.x1, line.y1)
                    : ""
                return "\(idx + 1)|\(geo)|\(String(format: "%.2f", line.confidence))|\(line.text)"
            }.joined(separator: "\n")
            try? bboxContent.write(to: bboxPath, atomically: true, encoding: .utf8)
            pageReport["lines"] = lines.count
            pageReport["words"] = lines.reduce(0) { $0 + $1.text.split(whereSeparator: { $0.isWhitespace }).count }
            pageReports.append(pageReport)
        }
    }

    fileReport["pages"] = doc.pageCount
    fileReport["pages_report"] = pageReports
    fileReport["total_ocr_ms"] = Int(totalOCRms)
    fileReport["status"] = anyFailure ? "partial" : "ok"
    writeReport(path: (outURL.appendingPathComponent("report.json")).path, report: fileReport)
    exit(anyFailure ? 3 : 0)
}

main()
