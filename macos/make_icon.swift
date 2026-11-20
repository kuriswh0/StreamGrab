// Renders the StreamGrab app icon (1024x1024 PNG): purple squircle + download arrow + chat bubble.
// Usage: make_icon out.png [--windows]
// --windows: same artwork, but filling the canvas and without the drop shadow, as Windows icons are drawn
// (the macOS version leaves a wide margin, which looks too small in Explorer and the taskbar).
import AppKit

let windows = CommandLine.arguments.contains("--windows")

let size: CGFloat = 1024
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()
let ctx = NSGraphicsContext.current!.cgContext
if windows {
    let k: CGFloat = 1000 / 824  // squircle 824 px -> 1000 px, centered
    ctx.translateBy(x: size / 2, y: size / 2); ctx.scaleBy(x: k, y: k); ctx.translateBy(x: -size / 2, y: -size / 2)
}

let inset: CGFloat = 100
let rect = CGRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let squircle = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
ctx.saveGState()
if !windows { ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 30, color: NSColor.black.withAlphaComponent(0.35).cgColor) }
NSColor(red: 0.57, green: 0.28, blue: 1.0, alpha: 1).setFill()
squircle.fill()
ctx.restoreGState()
NSGradient(starting: NSColor(red: 0.66, green: 0.42, blue: 1.0, alpha: 1),
           ending: NSColor(red: 0.42, green: 0.16, blue: 0.86, alpha: 1))!.draw(in: squircle, angle: -90)

// Chat bubble (back, upper right)
let bubble = NSBezierPath(roundedRect: CGRect(x: 520, y: 560, width: 290, height: 210), xRadius: 50, yRadius: 50)
bubble.move(to: CGPoint(x: 580, y: 575)); bubble.line(to: CGPoint(x: 560, y: 505)); bubble.line(to: CGPoint(x: 650, y: 565))
NSColor.white.withAlphaComponent(0.35).setFill(); bubble.fill()

// Download arrow
NSColor.white.setFill()
let shaft = NSBezierPath(roundedRect: CGRect(x: 437, y: 420, width: 150, height: 330), xRadius: 34, yRadius: 34)
shaft.fill()
let head = NSBezierPath()
head.move(to: CGPoint(x: 302, y: 470)); head.line(to: CGPoint(x: 722, y: 470)); head.line(to: CGPoint(x: 512, y: 250)); head.close()
head.lineJoinStyle = .round; head.lineWidth = 40; NSColor.white.setStroke(); head.stroke(); head.fill()
// Tray
let tray = NSBezierPath(roundedRect: CGRect(x: 262, y: 175, width: 500, height: 70), xRadius: 35, yRadius: 35)
tray.fill()
img.unlockFocus()

let rep = NSBitmapImageRep(data: img.tiffRepresentation!)!
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
