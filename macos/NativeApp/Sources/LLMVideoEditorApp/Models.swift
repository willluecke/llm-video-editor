import AppKit
import CoreGraphics
import Foundation

private func number(_ value: Any?, default fallback: Double = 0) -> Double {
    if let value = value as? NSNumber { return value.doubleValue }
    if let value = value as? String, let parsed = Double(value) { return parsed }
    return fallback
}

private func boolean(_ value: Any?, default fallback: Bool = false) -> Bool {
    if let value = value as? NSNumber { return value.boolValue }
    if let value = value as? Bool { return value }
    return fallback
}

private func string(_ value: Any?, default fallback: String = "") -> String {
    value as? String ?? fallback
}

struct TimelineTrack {
    let id: String
    let kind: String
    let muted: Bool
    let order: Int

    init?(_ dictionary: [String: Any], order: Int) {
        let id = string(dictionary["id"])
        guard !id.isEmpty else { return nil }
        self.id = id
        self.kind = string(dictionary["kind"], default: "video")
        self.muted = boolean(dictionary["muted"])
        self.order = order
    }
}

struct ClipTransform {
    let scale: Double
    let x: Double
    let y: Double
    let rotation: Double
    let cropTop: Double
    let cropRight: Double
    let cropBottom: Double
    let cropLeft: Double

    init(_ dictionary: [String: Any]) {
        scale = max(0.1, number(dictionary["scale"], default: 1))
        x = number(dictionary["x"])
        y = number(dictionary["y"])
        rotation = number(dictionary["rotation"])
        cropTop = max(0, number(dictionary["crop_top"]))
        cropRight = max(0, number(dictionary["crop_right"]))
        cropBottom = max(0, number(dictionary["crop_bottom"]))
        cropLeft = max(0, number(dictionary["crop_left"]))
    }
}

struct ClipColor {
    let brightness: Double
    let contrast: Double
    let saturation: Double
    let temperature: Double

    init(_ dictionary: [String: Any]) {
        brightness = min(1, max(-1, number(dictionary["brightness"])))
        contrast = min(3, max(0, number(dictionary["contrast"], default: 1)))
        saturation = min(3, max(0, number(dictionary["saturation"], default: 1)))
        temperature = min(1, max(-1, number(dictionary["temperature"])))
    }

    var signature: String { "\(brightness):\(contrast):\(saturation):\(temperature)" }
}

struct TimelineSegment {
    let id: String
    let kind: String
    let assetID: String
    let trackID: String
    let audioTrackID: String?
    let audioLinked: Bool
    let timelineStart: Double
    let sourceStart: Double
    let sourceEnd: Double
    let textDuration: Double
    let speed: Double
    let volume: Double
    let audioFadeIn: Double
    let audioFadeOut: Double
    let transitionInType: String
    let transitionInDuration: Double
    let transitionOutType: String
    let transitionOutDuration: Double
    let transform: ClipTransform
    let color: ClipColor
    let text: String
    let fontSize: Double
    let textColor: String
    let enabled: Bool

    init?(_ dictionary: [String: Any]) {
        guard boolean(dictionary["enabled"], default: true) else { return nil }
        let kind = string(dictionary["kind"], default: "video")
        guard ["video", "audio", "title", "caption"].contains(kind) else { return nil }
        id = string(dictionary["id"], default: UUID().uuidString)
        self.kind = kind
        assetID = string(dictionary["asset_id"])
        trackID = string(dictionary["track_id"])
        let rawAudioTrackID = string(dictionary["audio_track_id"])
        audioTrackID = rawAudioTrackID.isEmpty ? nil : rawAudioTrackID
        audioLinked = boolean(dictionary["audio_linked"])
        timelineStart = max(0, number(dictionary["timeline_start"]))
        sourceStart = max(0, number(dictionary["source_start"]))
        sourceEnd = max(sourceStart, number(dictionary["source_end"], default: sourceStart))
        textDuration = max(0.1, number(dictionary["duration"], default: 3))
        speed = min(8, max(0.1, number(dictionary["speed"], default: 1)))
        volume = min(2, max(0, number(dictionary["volume"], default: 1)))
        audioFadeIn = max(0, number(dictionary["audio_fade_in"]))
        audioFadeOut = max(0, number(dictionary["audio_fade_out"]))
        let transitionIn = dictionary["transition_in"] as? [String: Any] ?? [:]
        transitionInType = string(transitionIn["type"], default: "none")
        transitionInDuration = max(0, number(transitionIn["duration"]))
        let transitionOut = dictionary["transition_out"] as? [String: Any] ?? [:]
        transitionOutType = string(transitionOut["type"], default: "none")
        transitionOutDuration = max(0, number(transitionOut["duration"]))
        transform = ClipTransform(dictionary["transform"] as? [String: Any] ?? [:])
        color = ClipColor(dictionary["color"] as? [String: Any] ?? [:])
        text = string(dictionary["text"], default: string(dictionary["label"]))
        fontSize = min(240, max(12, number(dictionary["font_size"], default: kind == "caption" ? 72 : 64)))
        textColor = string(dictionary["text_color"], default: "#ffffff")
        enabled = true
    }

    var editDuration: Double {
        if kind == "title" || kind == "caption" { return textDuration }
        return max(0, sourceEnd - sourceStart) / speed
    }

    var timelineEnd: Double { timelineStart + editDuration }
}

struct TimelineSnapshot {
    let projectID: String
    let resolution: CGSize
    let fps: Double
    let tracks: [TimelineTrack]
    let segments: [TimelineSegment]

    init?(projectID: String, dictionary: [String: Any]) {
        guard !projectID.isEmpty else { return nil }
        self.projectID = projectID
        let rawResolution = dictionary["resolution"] as? [Any] ?? []
        let width = rawResolution.indices.contains(0) ? number(rawResolution[0], default: 1920) : 1920
        let height = rawResolution.indices.contains(1) ? number(rawResolution[1], default: 1080) : 1080
        resolution = CGSize(width: max(16, width), height: max(16, height))
        fps = min(120, max(1, number(dictionary["fps"], default: 30)))
        tracks = (dictionary["tracks"] as? [[String: Any]] ?? []).enumerated().compactMap {
            TimelineTrack($0.element, order: $0.offset)
        }
        segments = (dictionary["segments"] as? [[String: Any]] ?? []).compactMap(TimelineSegment.init)
    }

    var duration: Double {
        segments.reduce(0) { max($0, $1.timelineEnd) }
    }

    func track(_ id: String) -> TimelineTrack? {
        tracks.first { $0.id == id }
    }
}

extension NSColor {
    convenience init?(editorHex value: String) {
        var hex = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if hex.hasPrefix("#") { hex.removeFirst() }
        guard hex.count == 6, let raw = Int(hex, radix: 16) else { return nil }
        self.init(
            calibratedRed: CGFloat((raw >> 16) & 0xff) / 255,
            green: CGFloat((raw >> 8) & 0xff) / 255,
            blue: CGFloat(raw & 0xff) / 255,
            alpha: 1
        )
    }
}
