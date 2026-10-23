import AppKit
import AVFoundation
import QuartzCore

protocol CompositionEngineDelegate: AnyObject {
    func compositionEngine(_ engine: CompositionEngine, didUpdateTime time: Double, playing: Bool)
    func compositionEngine(_ engine: CompositionEngine, didChangeStatus message: String, isError: Bool)
}

private struct VideoLayerRecord {
    let segment: TimelineSegment
    let track: AVMutableCompositionTrack
    let instruction: AVMutableVideoCompositionLayerInstruction
    let trackOrder: Int
}

private struct PlaybackBuild {
    let item: AVPlayerItem
    let duration: Double
}

private final class VideoCompositionValidator: NSObject, AVVideoCompositionValidationHandling {
    var problems: [String] = []

    func videoComposition(
        _ videoComposition: AVVideoComposition,
        shouldContinueValidatingAfterFindingInvalidValueForKey key: String
    ) -> Bool {
        problems.append("invalid \(key)")
        return true
    }

    func videoComposition(
        _ videoComposition: AVVideoComposition,
        shouldContinueValidatingAfterFindingEmptyTimeRange timeRange: CMTimeRange
    ) -> Bool {
        problems.append("empty range \(CMTimeGetSeconds(timeRange.start))–\(CMTimeGetSeconds(timeRange.end))")
        return true
    }

    func videoComposition(
        _ videoComposition: AVVideoComposition,
        shouldContinueValidatingAfterFindingInvalidTimeRangeIn videoCompositionInstruction: any AVVideoCompositionInstructionProtocol
    ) -> Bool {
        let range = videoCompositionInstruction.timeRange
        problems.append("invalid instruction range \(CMTimeGetSeconds(range.start))–\(CMTimeGetSeconds(range.end))")
        return true
    }

    func videoComposition(
        _ videoComposition: AVVideoComposition,
        shouldContinueValidatingAfterFindingInvalidTrackIDIn videoCompositionInstruction: any AVVideoCompositionInstructionProtocol,
        layerInstruction: AVVideoCompositionLayerInstruction,
        asset: AVAsset
    ) -> Bool {
        problems.append("invalid track \(layerInstruction.trackID)")
        return true
    }
}

final class CompositionEngine: NSObject {
    let player = AVPlayer()
    weak var delegate: CompositionEngineDelegate?

    private let root: URL
    private var snapshot: TimelineSnapshot?
    private var generation = 0
    private var timeObserver: Any?
    private var endObserver: NSObjectProtocol?
    private var requestedTime = 0.0
    private var shouldResumeAfterBuild = false

    init(root: URL) {
        self.root = root
        super.init()
        player.actionAtItemEnd = .pause
        player.automaticallyWaitsToMinimizeStalling = false
        timeObserver = player.addPeriodicTimeObserver(
            forInterval: CMTime(value: 1, timescale: 30),
            queue: .main
        ) { [weak self] time in
            guard let self else { return }
            let seconds = time.seconds.isFinite ? max(0, time.seconds) : 0
            self.requestedTime = seconds
            self.delegate?.compositionEngine(self, didUpdateTime: seconds, playing: self.isPlaying)
        }
        endObserver = NotificationCenter.default.addObserver(
            forName: .AVPlayerItemDidPlayToEndTime,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self, notification.object as AnyObject? === self.player.currentItem else { return }
            self.delegate?.compositionEngine(self, didUpdateTime: self.duration, playing: false)
        }
    }

    deinit {
        if let timeObserver { player.removeTimeObserver(timeObserver) }
        if let endObserver { NotificationCenter.default.removeObserver(endObserver) }
    }

    var isPlaying: Bool { player.timeControlStatus == .playing }
    var duration: Double { snapshot?.duration ?? 0 }

    func update(_ snapshot: TimelineSnapshot, preferredTime: Double) {
        self.snapshot = snapshot
        generation += 1
        let buildGeneration = generation
        requestedTime = min(max(0, preferredTime), snapshot.duration)
        shouldResumeAfterBuild = isPlaying
        player.pause()
        delegate?.compositionEngine(self, didChangeStatus: "Building native preview…", isError: false)

        do {
            let build = try buildPlayback(snapshot)
            guard buildGeneration == generation else { return }
            player.replaceCurrentItem(with: build.item)
            seek(to: requestedTime, play: shouldResumeAfterBuild)
            delegate?.compositionEngine(self, didChangeStatus: "Native preview ready", isError: false)
        } catch {
            delegate?.compositionEngine(self, didChangeStatus: "Native preview failed: \(error.localizedDescription)", isError: true)
        }
    }

    func toggle(at time: Double) {
        if isPlaying {
            player.pause()
            delegate?.compositionEngine(self, didUpdateTime: player.currentTime().seconds, playing: false)
            return
        }
        let restart = duration > 0 && time >= duration - 0.02
        seek(to: restart ? 0 : time, play: true)
    }

    func seek(to seconds: Double, play: Bool) {
        let bounded = min(max(0, seconds), duration)
        requestedTime = bounded
        let target = mediaTime(bounded)
        player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] finished in
            guard let self, finished else { return }
            if play { self.player.play() }
            else { self.player.pause() }
            self.delegate?.compositionEngine(self, didUpdateTime: bounded, playing: play)
        }
    }

    func captureDiagnosticFrame(at seconds: Double, to url: URL) throws {
        guard let item = player.currentItem else {
            throw NSError(domain: "LLMVideoEditor", code: 3, userInfo: [NSLocalizedDescriptionKey: "No native player item is loaded"])
        }
        let generator = AVAssetImageGenerator(asset: item.asset)
        generator.videoComposition = item.videoComposition
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        let image = try generator.copyCGImage(at: mediaTime(seconds), actualTime: nil)
        let representation = NSBitmapImageRep(cgImage: image)
        guard let data = representation.representation(using: .png, properties: [:]) else {
            throw NSError(domain: "LLMVideoEditor", code: 4, userInfo: [NSLocalizedDescriptionKey: "Could not encode the native diagnostic frame"])
        }
        try data.write(to: url)
    }

    private func mediaTime(_ seconds: Double) -> CMTime {
        let finite = seconds.isFinite ? seconds : 0
        // Timeline math such as 9.12 + 1.70 can arrive as 10.819999999999999.
        // CMTime's Double initializer rounds that down by one 1/600-second tick,
        // leaving a microscopic hole before an adjacent clip at 10.82. Normalize
        // decimal editor times before conversion so adjoining cuts share one tick.
        let normalized = (finite * 1_000_000).rounded() / 1_000_000
        return CMTime(seconds: normalized, preferredTimescale: 600)
    }

    private func resolveAsset(_ assetID: String, projectID: String) -> URL? {
        guard !assetID.isEmpty, URL(fileURLWithPath: assetID).lastPathComponent == assetID else { return nil }
        let candidates = [
            root.appendingPathComponent("out/\(projectID)/\(assetID)"),
            root.appendingPathComponent("work/\(projectID)/media/\(assetID)"),
        ]
        return candidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }

    private func buildPlayback(_ snapshot: TimelineSnapshot) throws -> PlaybackBuild {
        let composition = AVMutableComposition()
        var videoRecords: [VideoLayerRecord] = []
        var audioParameters: [AVMutableAudioMixInputParameters] = []
        var assetCache: [String: AVURLAsset] = [:]

        func asset(for id: String) throws -> AVURLAsset {
            if let cached = assetCache[id] { return cached }
            guard let url = resolveAsset(id, projectID: snapshot.projectID) else {
                throw NSError(domain: "LLMVideoEditor", code: 1, userInfo: [NSLocalizedDescriptionKey: "Missing media: \(id)"])
            }
            let loaded = AVURLAsset(url: url, options: [AVURLAssetPreferPreciseDurationAndTimingKey: true])
            assetCache[id] = loaded
            return loaded
        }

        for segment in snapshot.segments where segment.kind == "video" {
            guard snapshot.track(segment.trackID)?.muted != true else { continue }
            let sourceAsset = try asset(for: segment.assetID)
            guard let sourceVideo = sourceAsset.tracks(withMediaType: .video).first,
                  let targetVideo = composition.addMutableTrack(
                    withMediaType: .video,
                    preferredTrackID: kCMPersistentTrackID_Invalid
                  ) else { continue }
            let sourceRange = CMTimeRange(
                start: mediaTime(segment.sourceStart),
                duration: mediaTime(segment.sourceEnd - segment.sourceStart)
            )
            let insertionTime = mediaTime(segment.timelineStart)
            try targetVideo.insertTimeRange(sourceRange, of: sourceVideo, at: insertionTime)
            scaleInsertedSegment(targetVideo, toDuration: mediaTime(segment.editDuration))

            let layerInstruction = AVMutableVideoCompositionLayerInstruction(assetTrack: targetVideo)
            let transform = displayTransform(
                sourceTrack: sourceVideo,
                segment: segment,
                renderSize: snapshot.resolution
            )
            layerInstruction.setTransform(transform, at: insertionTime)
            let crop = segment.transform
            if crop.cropTop > 0 || crop.cropRight > 0 || crop.cropBottom > 0 || crop.cropLeft > 0 {
                let size = sourceVideo.naturalSize
                let rectangle = CGRect(
                    x: size.width * crop.cropLeft / 100,
                    y: size.height * crop.cropBottom / 100,
                    width: size.width * max(0.01, 1 - (crop.cropLeft + crop.cropRight) / 100),
                    height: size.height * max(0.01, 1 - (crop.cropTop + crop.cropBottom) / 100)
                )
                layerInstruction.setCropRectangle(rectangle, at: insertionTime)
            }
            applyOpacityRamps(layerInstruction, segment: segment)
            videoRecords.append(VideoLayerRecord(
                segment: segment,
                track: targetVideo,
                instruction: layerInstruction,
                trackOrder: snapshot.track(segment.trackID)?.order ?? 0
            ))

            if segment.audioLinked,
               snapshot.track(segment.audioTrackID ?? "")?.muted != true,
               let sourceAudio = sourceAsset.tracks(withMediaType: .audio).first,
               let targetAudio = composition.addMutableTrack(
                    withMediaType: .audio,
                    preferredTrackID: kCMPersistentTrackID_Invalid
                ) {
                try targetAudio.insertTimeRange(sourceRange, of: sourceAudio, at: insertionTime)
                scaleInsertedSegment(targetAudio, toDuration: mediaTime(segment.editDuration))
                audioParameters.append(audioMixParameters(track: targetAudio, segment: segment))
            }
        }

        for segment in snapshot.segments where segment.kind == "audio" {
            guard snapshot.track(segment.trackID)?.muted != true else { continue }
            let sourceAsset = try asset(for: segment.assetID)
            guard let sourceAudio = sourceAsset.tracks(withMediaType: .audio).first,
                  let targetAudio = composition.addMutableTrack(
                    withMediaType: .audio,
                    preferredTrackID: kCMPersistentTrackID_Invalid
                  ) else { continue }
            let sourceRange = CMTimeRange(
                start: mediaTime(segment.sourceStart),
                duration: mediaTime(segment.sourceEnd - segment.sourceStart)
            )
            let insertionTime = mediaTime(segment.timelineStart)
            try targetAudio.insertTimeRange(sourceRange, of: sourceAudio, at: insertionTime)
            scaleInsertedSegment(targetAudio, toDuration: mediaTime(segment.editDuration))
            audioParameters.append(audioMixParameters(track: targetAudio, segment: segment))
        }

        if composition.duration.seconds < snapshot.duration {
            composition.insertEmptyTimeRange(CMTimeRange(
                start: composition.duration,
                duration: mediaTime(snapshot.duration - composition.duration.seconds)
            ))
        }

        let playerItem = AVPlayerItem(asset: composition)
        if !videoRecords.isEmpty {
            let videoComposition = makeVideoComposition(snapshot: snapshot, records: videoRecords)
            let fullRange = CMTimeRange(start: .zero, duration: mediaTime(snapshot.duration))
            let validator = VideoCompositionValidator()
            guard videoComposition.isValid(for: composition, timeRange: fullRange, validationDelegate: validator) else {
                let detail = validator.problems.isEmpty ? "unknown validation error" : validator.problems.joined(separator: ", ")
                throw NSError(domain: "LLMVideoEditor", code: 5, userInfo: [NSLocalizedDescriptionKey: "The native video composition is invalid (\(detail))"])
            }
            playerItem.videoComposition = videoComposition
        }
        if !audioParameters.isEmpty {
            let audioMix = AVMutableAudioMix()
            audioMix.inputParameters = audioParameters
            playerItem.audioMix = audioMix
        }
        return PlaybackBuild(item: playerItem, duration: snapshot.duration)
    }

    private func scaleInsertedSegment(_ track: AVMutableCompositionTrack, toDuration duration: CMTime) {
        guard let insertedRange = track.segments.last?.timeMapping.target,
              insertedRange.isValid,
              !insertedRange.isEmpty else { return }
        track.scaleTimeRange(insertedRange, toDuration: duration)
    }

    private func displayTransform(
        sourceTrack: AVAssetTrack,
        segment: TimelineSegment,
        renderSize: CGSize
    ) -> CGAffineTransform {
        let naturalSize = sourceTrack.naturalSize
        var transform = sourceTrack.preferredTransform
        let transformedRect = CGRect(origin: .zero, size: naturalSize).applying(transform)
        transform = transform.concatenating(CGAffineTransform(
            translationX: -transformedRect.minX,
            y: -transformedRect.minY
        ))
        let orientedSize = CGSize(width: abs(transformedRect.width), height: abs(transformedRect.height))
        let fit = min(renderSize.width / max(1, orientedSize.width), renderSize.height / max(1, orientedSize.height))
        let scale = fit * segment.transform.scale
        transform = transform.concatenating(CGAffineTransform(scaleX: scale, y: scale))
        let displayed = CGSize(width: orientedSize.width * scale, height: orientedSize.height * scale)
        transform = transform.concatenating(CGAffineTransform(
            translationX: (renderSize.width - displayed.width) / 2 + segment.transform.x,
            y: (renderSize.height - displayed.height) / 2 - segment.transform.y
        ))
        if segment.transform.rotation.truncatingRemainder(dividingBy: 360) != 0 {
            let center = CGPoint(x: renderSize.width / 2, y: renderSize.height / 2)
            let rotation = CGAffineTransform(translationX: center.x, y: center.y)
                .rotated(by: CGFloat(segment.transform.rotation * .pi / 180))
                .translatedBy(x: -center.x, y: -center.y)
            transform = transform.concatenating(rotation)
        }
        return transform
    }

    private func applyOpacityRamps(
        _ instruction: AVMutableVideoCompositionLayerInstruction,
        segment: TimelineSegment
    ) {
        let start = mediaTime(segment.timelineStart)
        let end = mediaTime(segment.timelineEnd)
        if segment.transitionInType != "none", segment.transitionInDuration > 0 {
            let duration = min(segment.transitionInDuration, segment.editDuration)
            instruction.setOpacityRamp(
                fromStartOpacity: 0,
                toEndOpacity: 1,
                timeRange: CMTimeRange(start: start, duration: mediaTime(duration))
            )
        } else {
            instruction.setOpacity(1, at: start)
        }
        if segment.transitionOutType != "none", segment.transitionOutDuration > 0 {
            let duration = min(segment.transitionOutDuration, segment.editDuration)
            instruction.setOpacityRamp(
                fromStartOpacity: 1,
                toEndOpacity: 0,
                timeRange: CMTimeRange(start: mediaTime(segment.timelineEnd - duration), duration: mediaTime(duration))
            )
        }
        instruction.setOpacity(0, at: end)
    }

    private func audioMixParameters(
        track: AVCompositionTrack,
        segment: TimelineSegment
    ) -> AVMutableAudioMixInputParameters {
        let parameters = AVMutableAudioMixInputParameters(track: track)
        let start = mediaTime(segment.timelineStart)
        let duration = segment.editDuration
        parameters.setVolume(Float(segment.volume), at: start)
        if segment.audioFadeIn > 0 {
            parameters.setVolumeRamp(
                fromStartVolume: 0,
                toEndVolume: Float(segment.volume),
                timeRange: CMTimeRange(start: start, duration: mediaTime(min(segment.audioFadeIn, duration)))
            )
        }
        if segment.audioFadeOut > 0 {
            let fade = min(segment.audioFadeOut, duration)
            parameters.setVolumeRamp(
                fromStartVolume: Float(segment.volume),
                toEndVolume: 0,
                timeRange: CMTimeRange(start: mediaTime(segment.timelineEnd - fade), duration: mediaTime(fade))
            )
        }
        return parameters
    }

    private func makeVideoComposition(
        snapshot: TimelineSnapshot,
        records: [VideoLayerRecord]
    ) -> AVMutableVideoComposition {
        let videoComposition = AVMutableVideoComposition()
        videoComposition.renderSize = snapshot.resolution
        videoComposition.frameDuration = CMTime(value: 1, timescale: CMTimeScale(round(snapshot.fps)))

        let boundaries = Set([0, snapshot.duration] + records.flatMap { [$0.segment.timelineStart, $0.segment.timelineEnd] }).sorted()
        var instructions: [AVVideoCompositionInstructionProtocol] = []
        for index in 0..<(max(0, boundaries.count - 1)) {
            let start = boundaries[index]
            let end = boundaries[index + 1]
            guard end - start > 0.0001 else { continue }
            let active = records
                .filter { $0.segment.timelineStart < end && $0.segment.timelineEnd > start }
                .sorted {
                    if $0.trackOrder != $1.trackOrder { return $0.trackOrder > $1.trackOrder }
                    return $0.segment.timelineStart > $1.segment.timelineStart
                }
            let instruction = AVMutableVideoCompositionInstruction()
            let startTime = mediaTime(start)
            let endTime = mediaTime(end)
            instruction.timeRange = CMTimeRange(start: startTime, duration: CMTimeSubtract(endTime, startTime))
            instruction.layerInstructions = active.map(\.instruction)
            instruction.backgroundColor = NSColor.black.cgColor
            instruction.enablePostProcessing = true
            instructions.append(instruction)
        }
        videoComposition.instructions = instructions
        return videoComposition
    }
}
