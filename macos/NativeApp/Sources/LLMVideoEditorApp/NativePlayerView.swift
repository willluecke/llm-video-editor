import AppKit
import AVFoundation

final class NativePlayerView: NSView {
    let playerLayer = AVPlayerLayer()
    private let titleLayer = CATextLayer()
    private let captionBackgroundLayer = CALayer()
    private let captionLayer = CATextLayer()
    private var snapshot: TimelineSnapshot?
    private var currentTime = 0.0

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer = playerLayer
        playerLayer.backgroundColor = NSColor.black.cgColor
        playerLayer.videoGravity = .resizeAspect
        playerLayer.needsDisplayOnBoundsChange = true
        configureTextLayer(titleLayer)
        configureTextLayer(captionLayer)
        captionLayer.truncationMode = .none
        captionBackgroundLayer.backgroundColor = NSColor.black.withAlphaComponent(0.70).cgColor
        captionBackgroundLayer.cornerRadius = 8
        captionBackgroundLayer.isHidden = true
        playerLayer.addSublayer(titleLayer)
        playerLayer.addSublayer(captionBackgroundLayer)
        playerLayer.addSublayer(captionLayer)
    }

    required init?(coder: NSCoder) {
        nil
    }

    var player: AVPlayer? {
        get { playerLayer.player }
        set { playerLayer.player = newValue }
    }

    override func layout() {
        super.layout()
        updateOverlays(at: currentTime)
    }

    func configure(_ snapshot: TimelineSnapshot) {
        self.snapshot = snapshot
        updateOverlays(at: currentTime)
    }

    func updateOverlays(at time: Double) {
        currentTime = time
        guard let snapshot else {
            titleLayer.isHidden = true
            captionLayer.isHidden = true
            captionBackgroundLayer.isHidden = true
            return
        }
        let textSegments = snapshot.segments.filter {
            ($0.kind == "title" || $0.kind == "caption") && time >= $0.timelineStart && time < $0.timelineEnd
        }
        updateTitle(titleLayer, with: textSegments.last { $0.kind == "title" }, snapshot: snapshot)
        updateCaption(with: textSegments.last { $0.kind == "caption" }, snapshot: snapshot)
    }

    private func configureTextLayer(_ layer: CATextLayer) {
        layer.contentsScale = NSScreen.main?.backingScaleFactor ?? 2
        layer.alignmentMode = .center
        layer.isWrapped = true
        layer.truncationMode = .end
        layer.shadowColor = NSColor.black.cgColor
        layer.shadowOpacity = 0.9
        layer.shadowRadius = 3
        layer.shadowOffset = CGSize(width: 0, height: -1)
        layer.isHidden = true
    }

    private func updateTitle(
        _ layer: CATextLayer,
        with segment: TimelineSegment?,
        snapshot: TimelineSnapshot
    ) {
        guard let segment, bounds.width > 0, bounds.height > 0 else {
            layer.isHidden = true
            return
        }
        let scale = min(bounds.width / snapshot.resolution.width, bounds.height / snapshot.resolution.height)
        let fontSize = max(12, CGFloat(segment.fontSize) * scale)
        let height = max(40, fontSize * 2.4)
        layer.string = segment.text
        layer.font = NSFont.systemFont(ofSize: fontSize, weight: .bold)
        layer.fontSize = fontSize
        layer.foregroundColor = (NSColor(editorHex: segment.textColor) ?? .white).cgColor
        layer.cornerRadius = 0
        layer.frame = CGRect(
            x: bounds.width * 0.06,
            y: bounds.height - height - bounds.height * 0.07,
            width: bounds.width * 0.88,
            height: height
        )
        layer.isHidden = false
    }

    private func updateCaption(with segment: TimelineSegment?, snapshot: TimelineSnapshot) {
        guard let segment, bounds.width > 0, bounds.height > 0 else {
            captionLayer.isHidden = true
            captionBackgroundLayer.isHidden = true
            return
        }
        let scale = min(bounds.width / snapshot.resolution.width, bounds.height / snapshot.resolution.height)
        let fontSize = max(12, CGFloat(segment.fontSize) * scale)
        let font = NSFont.systemFont(ofSize: fontSize, weight: .semibold)
        let backgroundX = bounds.width * 0.06
        let backgroundWidth = bounds.width * 0.88
        let horizontalPadding = max(8, 14 * scale)
        let verticalPadding = max(6, 14 * scale)
        let textWidth = max(1, backgroundWidth - horizontalPadding * 2)
        let textColor = NSColor(editorHex: segment.textColor) ?? .white
        let paragraphStyle = NSMutableParagraphStyle()
        paragraphStyle.alignment = .center
        paragraphStyle.lineBreakMode = .byWordWrapping
        let attributedText = NSAttributedString(
            string: segment.text,
            attributes: [
                .font: font,
                .foregroundColor: textColor,
                .paragraphStyle: paragraphStyle,
            ]
        )
        let textBounds = attributedText.boundingRect(
            with: CGSize(width: textWidth, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading],
            context: nil
        )
        let textHeight = max(font.ascender - font.descender + font.leading, ceil(textBounds.height))
        let backgroundHeight = textHeight + verticalPadding * 2
        let backgroundY = bounds.height * 0.14

        captionBackgroundLayer.frame = CGRect(
            x: backgroundX,
            y: backgroundY,
            width: backgroundWidth,
            height: backgroundHeight
        )
        captionBackgroundLayer.isHidden = false

        captionLayer.string = attributedText
        captionLayer.font = font
        captionLayer.fontSize = fontSize
        captionLayer.foregroundColor = textColor.cgColor
        captionLayer.frame = CGRect(
            x: backgroundX + horizontalPadding,
            y: backgroundY + verticalPadding,
            width: textWidth,
            height: textHeight
        )
        captionLayer.isHidden = false
    }
}
