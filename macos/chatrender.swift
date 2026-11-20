// chatrender: draws a Twitch-style chat panel and either
//   * streams raw BGRA (premultiplied) frames to stdout for ffmpeg to composite, or
//   * writes a single PNG frame (--preview-at SECONDS --out file.png) for the style editor.
//
//   chatrender --input render.json --width W --height H --duration S [--fps 15] [--cache DIR]
//   chatrender --input render.json --width W --height H --preview-at 12.5 --out frame.png
//   chatrender --list-fonts
//
// render.json: {
//   "style": {font_family, font_size, text_color, bg_color, bg_opacity, radius, show_badges,
//             show_emotes, animate, timestamps, shadow},
//   "badges": {"subscriber/12": url, ...},
//   "messages": [{"t", "ts", "user", "color", "badges": ["setID/version"], "parts": [{"text"} | {"emote","url"}]}]
// }
import AppKit
import Foundation
import ImageIO

// ---------------------------------------------------------------- input
struct Part: Decodable { let text: String?; let emote: String?; let url: String? }
struct Msg: Decodable {
    let t: Double; let ts: String?; let user: String; let color: String?
    let badges: [String]?; let parts: [Part]
}
struct Style: Decodable {
    var font_family: String? = nil; var font_size: Double? = nil; var text_color: String? = nil
    var bg_color: String? = nil; var bg_opacity: Double? = nil; var radius: Double? = nil
    var show_badges: Bool? = nil; var show_emotes: Bool? = nil; var animate: Bool? = nil
    var timestamps: Bool? = nil; var shadow: Bool? = nil
}
struct Input: Decodable { let style: Style?; let badges: [String: String]?; let messages: [Msg] }

var opts: [String: String] = [:]
var argv = CommandLine.arguments.dropFirst().makeIterator()
while let k = argv.next() {
    guard k.hasPrefix("--") else { continue }
    let key = String(k.dropFirst(2))
    if key == "list-fonts" { opts[key] = "1"; continue }
    if let v = argv.next() { opts[key] = v }
}
func num(_ k: String, _ d: Double) -> Double { Double(opts[k] ?? "") ?? d }

if opts["list-fonts"] != nil {
    let fams = NSFontManager.shared.availableFontFamilies.filter { !$0.hasPrefix(".") }
    print(String(data: try! JSONSerialization.data(withJSONObject: fams), encoding: .utf8)!)
    exit(0)
}
guard let inputPath = opts["input"] else {
    FileHandle.standardError.write("usage: chatrender --input render.json --width W --height H (--duration S | --preview-at T --out f.png)\n".data(using: .utf8)!)
    exit(2)
}
let W = Int(num("width", 400)), H = Int(num("height", 1080))
let fps = num("fps", 15), duration = num("duration", 60)
let previewAt = opts["preview-at"].flatMap(Double.init)
let cacheDir = URL(fileURLWithPath: opts["cache"] ?? NSTemporaryDirectory() + "streamgrab-emotes")
try? FileManager.default.createDirectory(at: cacheDir, withIntermediateDirectories: true)

let input = try JSONDecoder().decode(Input.self, from: Data(contentsOf: URL(fileURLWithPath: inputPath)))
let msgs = input.messages
let times = msgs.map(\.t)
let style = input.style ?? Style()
let F = CGFloat(style.font_size ?? 22)
let showBadges = style.show_badges ?? true, showEmotes = style.show_emotes ?? true
let animate = style.animate ?? true, showTimestamps = style.timestamps ?? false

// ---------------------------------------------------------------- images (emotes + badges)
/// A decoded image; animated GIF/WebP keep every frame with its delay.
final class Picture {
    let frames: [CGImage]; let delays: [Double]; let total: Double
    let size: CGSize
    init?(data: Data, still: Bool = false) {
        guard let src = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let n = still ? 1 : CGImageSourceGetCount(src)  // URL ending in #still: first frame only
        var fr: [CGImage] = [], de: [Double] = []
        for i in 0..<max(n, 1) {
            guard let img = CGImageSourceCreateImageAtIndex(src, i, nil) else { continue }
            fr.append(img)
            let props = CGImageSourceCopyPropertiesAtIndex(src, i, nil) as? [CFString: Any] ?? [:]
            var d = 0.1
            for key in [kCGImagePropertyGIFDictionary, kCGImagePropertyWebPDictionary, kCGImagePropertyPNGDictionary] {
                if let dict = props[key] as? [CFString: Any] {
                    let v = (dict[kCGImagePropertyGIFUnclampedDelayTime] ?? dict[kCGImagePropertyWebPUnclampedDelayTime]
                             ?? dict[kCGImagePropertyGIFDelayTime] ?? dict[kCGImagePropertyWebPDelayTime]
                             ?? dict[kCGImagePropertyAPNGUnclampedDelayTime] ?? dict[kCGImagePropertyAPNGDelayTime]) as? Double
                    if let v, v > 0.011 { d = v }
                }
            }
            de.append(d)
        }
        guard let first = fr.first else { return nil }
        frames = fr; delays = de; total = de.reduce(0, +)
        size = CGSize(width: first.width, height: first.height)
    }
    var animated: Bool { frames.count > 1 }
    func frame(at t: Double) -> CGImage {
        guard animate, animated, total > 0 else { return frames[0] }
        var x = t.truncatingRemainder(dividingBy: total)
        for (i, d) in delays.enumerated() { if x < d { return frames[i] }; x -= d }
        return frames[frames.count - 1]
    }
}

func hiRes(_ url: String) -> String {
    url.contains("static-cdn.jtvnw.net/emoticons") ? url.replacingOccurrences(of: "/1.0", with: "/2.0") : url
}
var pictures: [String: Picture] = [:]
do {
    var urls = Set<String>()
    if showEmotes { msgs.forEach { $0.parts.forEach { if let u = $0.url, !u.isEmpty { urls.insert(u) } } } }
    if showBadges { (input.badges ?? [:]).values.forEach { urls.insert($0) } }
    let lock = NSLock(), group = DispatchGroup(), gate = DispatchSemaphore(value: 16)
    let session = URLSession(configuration: .ephemeral)
    for url in urls {
        let name = url.unicodeScalars.map { CharacterSet.alphanumerics.contains($0) ? String($0) : "_" }.joined()
        let file = cacheDir.appendingPathComponent(String(name.suffix(140)) + ".img")
        let still = url.hasSuffix("#still")
        if let data = try? Data(contentsOf: file), let p = Picture(data: data, still: still) { pictures[url] = p; continue }
        guard let remote = URL(string: hiRes(url)) else { continue }
        group.enter(); gate.wait()
        session.dataTask(with: remote) { data, resp, _ in
            defer { gate.signal(); group.leave() }
            guard let data, (resp as? HTTPURLResponse)?.statusCode == 200, let p = Picture(data: data, still: still) else { return }
            try? data.write(to: file)
            lock.lock(); pictures[url] = p; lock.unlock()
        }.resume()
    }
    _ = group.wait(timeout: .now() + 90)
}

// ---------------------------------------------------------------- styling
func parseHex(_ hex: String?) -> NSColor? {
    guard var s = hex?.trimmingCharacters(in: .whitespaces) else { return nil }
    if s.hasPrefix("#") { s.removeFirst() }
    guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
    return NSColor(srgbRed: CGFloat(v >> 16 & 0xff) / 255, green: CGFloat(v >> 8 & 0xff) / 255,
                   blue: CGFloat(v & 0xff) / 255, alpha: 1)
}

func makeFont(bold: Bool) -> NSFont {
    let fallback = NSFont.systemFont(ofSize: F, weight: bold ? .bold : .medium)
    guard let fam = style.font_family, !fam.isEmpty, fam != "System" else { return fallback }
    let fm = NSFontManager.shared
    if let f = fm.font(withFamily: fam, traits: bold ? .boldFontMask : [], weight: bold ? 9 : 5, size: F) { return f }
    if let f = NSFont(name: fam, size: F) { return bold ? fm.convert(f, toHaveTrait: .boldFontMask) : f }
    return fallback
}
let font = makeFont(bold: false), bold = makeFont(bold: true)
let textColor = parseHex(style.text_color) ?? NSColor(srgbRed: 0.94, green: 0.94, blue: 0.95, alpha: 1)
let bgColor = (parseHex(style.bg_color) ?? NSColor(srgbRed: 0.094, green: 0.094, blue: 0.106, alpha: 1))
    .withAlphaComponent(CGFloat(max(0, min(100, style.bg_opacity ?? 100))) / 100)
let radius = CGFloat(style.radius ?? 0)
let shadow: NSShadow? = (style.shadow ?? false) ? {
    let s = NSShadow(); s.shadowColor = NSColor(white: 0, alpha: 0.95)
    s.shadowBlurRadius = max(1.5, F * 0.14); s.shadowOffset = NSSize(width: 0, height: -max(1, F * 0.05)); return s
}() : nil
let para: NSParagraphStyle = {
    let p = NSMutableParagraphStyle(); p.lineBreakMode = .byWordWrapping; p.lineSpacing = F * 0.12; return p
}()
let palette = ["#FF4A4A", "#5C7CFF", "#2ECC71", "#FF7F50", "#9ACD32", "#FF6A3D", "#3CB371",
               "#DAA520", "#E07B39", "#5F9EA0", "#1E90FF", "#FF69B4", "#A970FF", "#00D68F"]

func userColor(_ m: Msg) -> NSColor {
    let c = parseHex(m.color) ?? parseHex(palette[abs(m.user.unicodeScalars.reduce(0) { $0 &+ Int($1.value) }) % palette.count])!
    let lum = 0.2126 * c.redComponent + 0.7152 * c.greenComponent + 0.0722 * c.blueComponent
    return lum < 0.35 ? c.blended(withFraction: 0.45, of: .white) ?? c : c
}

// Letter pills when a real badge image is unavailable.
let pillStyles: [String: (String, NSColor)] = [
    "broadcaster": ("★", NSColor(srgbRed: 0.91, green: 0.10, blue: 0.09, alpha: 1)),
    "moderator": ("M", NSColor(srgbRed: 0.0, green: 0.68, blue: 0.01, alpha: 1)),
    "vip": ("V", NSColor(srgbRed: 0.88, green: 0.02, blue: 0.73, alpha: 1)),
    "subscriber": ("S", NSColor(srgbRed: 0.57, green: 0.28, blue: 1.0, alpha: 1)),
    "founder": ("F", NSColor(srgbRed: 0.57, green: 0.28, blue: 1.0, alpha: 1)),
    "verified": ("✓", NSColor(srgbRed: 0.57, green: 0.28, blue: 1.0, alpha: 1)),
    "member": ("M", NSColor(srgbRed: 0.16, green: 0.6, blue: 0.3, alpha: 1)),
]
var pillCache: [String: Picture] = [:]
func pill(_ set: String) -> Picture? {
    if let p = pillCache[set] { return p }
    guard let (label, color) = pillStyles[set] else { return nil }
    let side = Int(round(F * 0.95 * 2))  // 2x for crispness
    guard let c = CGContext(data: nil, width: side, height: side, bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
    else { return nil }
    let prev = NSGraphicsContext.current
    NSGraphicsContext.current = NSGraphicsContext(cgContext: c, flipped: false)
    let r = CGRect(x: 0, y: 0, width: side, height: side)
    color.setFill(); NSBezierPath(roundedRect: r, xRadius: CGFloat(side) * 0.22, yRadius: CGFloat(side) * 0.22).fill()
    let s = NSAttributedString(string: label, attributes: [.font: NSFont.systemFont(ofSize: CGFloat(side) * 0.66, weight: .heavy), .foregroundColor: NSColor.white])
    let sz = s.size(); s.draw(at: NSPoint(x: (r.width - sz.width) / 2, y: (r.height - sz.height) / 2))
    NSGraphicsContext.current = prev
    guard let img = c.makeImage(), let png = NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:]),
          let p = Picture(data: png) else { return nil }
    pillCache[set] = p
    return p
}

// ---------------------------------------------------------------- message layout
/// A laid-out message: text (with blank placeholders where images go) plus image rects.
struct Laid { let storage: NSTextStorage; let lm: NSLayoutManager; let height: CGFloat
              let images: [(Picture, CGRect)]; let animated: Bool }

let padX = round(F * 0.6), gap = round(F * 0.3)
let contentWidth = max(20, CGFloat(W) - 2 * padX)
let blank = NSImage(size: NSSize(width: 1, height: 1))

func placeholder(_ p: Picture, height: CGFloat, baseline: CGFloat) -> NSAttributedString {
    let a = NSTextAttachment()
    a.image = blank
    let w = p.size.height > 0 ? p.size.width / p.size.height * height : height
    a.bounds = CGRect(x: 0, y: baseline, width: w, height: height)
    return NSAttributedString(attachment: a)
}

var laidCache: [Int: Laid] = [:]
func laid(_ i: Int) -> Laid {
    if let l = laidCache[i] { return l }
    let m = msgs[i]
    var base: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: textColor, .paragraphStyle: para]
    if let shadow { base[.shadow] = shadow }
    let s = NSMutableAttributedString()
    var pics: [Int: Picture] = [:]  // character index -> picture
    func addPic(_ p: Picture, _ h: CGFloat, _ baseline: CGFloat) {
        pics[s.length] = p
        s.append(placeholder(p, height: h, baseline: baseline))
    }
    if showTimestamps, let ts = m.ts {
        var a = base; a[.foregroundColor] = textColor.withAlphaComponent(0.55)
        a[.font] = NSFont.monospacedDigitSystemFont(ofSize: F * 0.8, weight: .regular)
        s.append(NSAttributedString(string: ts + " ", attributes: a))
    }
    if showBadges {
        for b in m.badges ?? [] {
            let set = String(b.split(separator: "/").first ?? "")
            if let url = input.badges?[b] ?? input.badges?[set + "/1"], let p = pictures[url] {
                addPic(p, round(F * 0.95), -F * 0.14)
            } else if let p = pill(set) {
                addPic(p, round(F * 0.95), -F * 0.14)
            } else { continue }
            s.append(NSAttributedString(string: " ", attributes: base))
        }
    }
    var nameAttrs = base; nameAttrs[.font] = bold; nameAttrs[.foregroundColor] = userColor(m)
    s.append(NSAttributedString(string: m.user, attributes: nameAttrs))
    s.append(NSAttributedString(string: ": ", attributes: base))
    for p in m.parts {
        if showEmotes, let url = p.url, let pic = pictures[url] {
            addPic(pic, round(F * 1.45), -F * 0.38)
        } else {
            s.append(NSAttributedString(string: p.text ?? p.emote ?? "", attributes: base))
        }
    }
    s.addAttribute(.paragraphStyle, value: para, range: NSRange(location: 0, length: s.length))

    let storage = NSTextStorage(attributedString: s)
    let container = NSTextContainer(size: NSSize(width: contentWidth, height: .greatestFiniteMagnitude))
    container.lineFragmentPadding = 0
    let lm = NSLayoutManager()
    lm.addTextContainer(container)
    storage.addLayoutManager(lm)
    lm.ensureLayout(for: container)
    var images: [(Picture, CGRect)] = []
    for (idx, pic) in pics {
        let glyphs = lm.glyphRange(forCharacterRange: NSRange(location: idx, length: 1), actualCharacterRange: nil)
        let frag = lm.lineFragmentRect(forGlyphAt: glyphs.location, effectiveRange: nil)
        let loc = lm.location(forGlyphAt: glyphs.location)  // baseline position within the fragment
        guard let att = storage.attribute(.attachment, at: idx, effectiveRange: nil) as? NSTextAttachment else { continue }
        let b = att.bounds
        // Flipped coordinates: top of image = baseline - (bounds.origin.y + height)
        images.append((pic, CGRect(x: frag.minX + loc.x, y: frag.minY + loc.y - b.origin.y - b.height,
                                   width: b.width, height: b.height)))
    }
    let height = ceil(lm.usedRect(for: container).height)
    let l = Laid(storage: storage, lm: lm, height: height, images: images,
                 animated: animate && images.contains { $0.0.animated })
    laidCache[i] = l
    if laidCache.count > 3000 { laidCache.removeAll(keepingCapacity: true) }  // bound memory on huge chats
    return l
}

// ---------------------------------------------------------------- frames
let bytesPerRow = W * 4
guard let ctx = CGContext(data: nil, width: W, height: H, bitsPerComponent: 8, bytesPerRow: bytesPerRow,
                          space: CGColorSpace(name: CGColorSpace.sRGB)!,
                          bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
else { exit(1) }
// Top-left origin, like a text view.
ctx.translateBy(x: 0, y: CGFloat(H)); ctx.scaleBy(x: 1, y: -1)
let gctx = NSGraphicsContext(cgContext: ctx, flipped: true)
NSGraphicsContext.current = gctx

/// Renders chat state at time t; returns whether any visible emote is animated.
func render(upTo k: Int, at t: Double) -> Bool {
    let full = CGRect(x: 0, y: 0, width: W, height: H)
    ctx.clear(full)
    if bgColor.alphaComponent > 0 {
        bgColor.setFill()
        radius > 0 ? NSBezierPath(roundedRect: full, xRadius: radius, yRadius: radius).fill() : full.fill()
    }
    ctx.saveGState()
    ctx.clip(to: full.insetBy(dx: 0, dy: min(radius * 0.4, F * 0.4)))
    var bottom = CGFloat(H) - round(F * 0.5)
    var anyAnimated = false
    var i = k
    while i >= 0 && bottom > 0 {
        let l = laid(i)
        let top = bottom - l.height
        if top < round(F * 0.3) && i != k { break }  // don't show a message cut off at the top edge
        let origin = CGPoint(x: padX, y: top)
        l.lm.drawGlyphs(forGlyphRange: l.lm.glyphRange(for: l.lm.textContainers[0]), at: origin)
        for (pic, r) in l.images {
            let rect = r.offsetBy(dx: origin.x, dy: origin.y)
            ctx.saveGState()
            // Undo the flip locally so the CGImage draws upright.
            ctx.translateBy(x: 0, y: rect.maxY); ctx.scaleBy(x: 1, y: -1)
            ctx.interpolationQuality = .high
            ctx.draw(pic.frame(at: t), in: CGRect(x: rect.minX, y: 0, width: rect.width, height: rect.height))
            ctx.restoreGState()
        }
        anyAnimated = anyAnimated || l.animated
        bottom = top - gap
        i -= 1
    }
    ctx.restoreGState()
    gctx.flushGraphics()
    return anyAnimated
}

func lastIndex(atOrBefore t: Double) -> Int {
    var lo = 0, hi = times.count
    while lo < hi { let mid = (lo + hi) / 2; if times[mid] <= t { lo = mid + 1 } else { hi = mid } }
    return lo - 1
}

if let t = previewAt {
    // --preview-frames N --preview-fps F: if an animated emote is visible, also write N-1 more frames
    // (out-1.png, out-2.png, ...) so the style editor can play the animation.
    let k = lastIndex(atOrBefore: t)
    let moving = render(upTo: k, at: t)
    guard let out = opts["out"] else { exit(1) }
    func save(_ path: String) {
        guard let img = ctx.makeImage(),
              let png = NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:]) else { exit(1) }
        try? png.write(to: URL(fileURLWithPath: path))
    }
    save(out)
    let extra = moving ? (opts["preview-frames"].flatMap(Int.init) ?? 1) - 1 : 0
    let pfps = opts["preview-fps"].flatMap(Double.init) ?? 12
    if extra > 0 {
        let base = (out as NSString).deletingPathExtension
        for i in 1...extra {
            _ = render(upTo: k, at: t + Double(i) / pfps)
            save("\(base)-\(i).png")
        }
    }
    exit(0)
}

signal(SIGPIPE, SIG_IGN)
let frameCount = Int((duration * fps).rounded(.up))
let frameBytes = bytesPerRow * H
var shown = Int.min, animating = false
for f in 0..<frameCount {
    let t = Double(f) / fps
    let k = lastIndex(atOrBefore: t)
    if k != shown || animating {
        autoreleasepool { animating = render(upTo: k, at: t) }
        shown = k
    }
    if fwrite(ctx.data!, 1, frameBytes, stdout) != frameBytes { exit(0) }  // ffmpeg closed the pipe
    if f % Int(max(1, fps * 10)) == 0 { FileHandle.standardError.write("frame \(f)/\(frameCount)\n".data(using: .utf8)!) }
}
fflush(stdout)
