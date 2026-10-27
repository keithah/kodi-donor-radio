import AVFoundation
import CoreMedia
import Darwin

struct CaptureTimestamp: Codable, Equatable, Sendable {
    let value: Int64
    let timescale: Int32
    let epoch: Int64
    let flags: UInt32
    init(_ time: CMTime) {
        value = time.value
        timescale = time.timescale
        epoch = time.epoch
        flags = time.flags.rawValue
    }
}

struct PCMFormatSnapshot: Codable, Equatable, Sendable {
    let sampleRate: Double
    let channels: UInt32
    let formatID: UInt32
    let formatFlags: UInt32
    let bytesPerFrame: UInt32
    let bitsPerChannel: UInt32
    let interleaved: Bool
    init(_ format: AVAudioFormat) {
        let description = format.streamDescription.pointee
        sampleRate = description.mSampleRate
        channels = description.mChannelsPerFrame
        formatID = description.mFormatID
        formatFlags = description.mFormatFlags
        bytesPerFrame = description.mBytesPerFrame
        bitsPerChannel = description.mBitsPerChannel
        interleaved = format.isInterleaved
    }
}

struct CaptureSummary: Encodable, Sendable {
    let schemaVersion = 2
    let transport = "core_audio_process_tap"
    var status: String
    let target: KodiProcess
    let requestedDuration: Double
    let startedAt: String
    let startedHostNS: UInt64
    var captureStartedHostNS: UInt64?
    var endedAt: String?
    var endedHostNS: UInt64?
    var stopReason: String?
    var error: String?
    var totalFrames: Int64 = 0
    var audioBuffers: Int = 0
    var discontinuities: Int = 0
    var peakMagnitude: Double = 0
    var format: PCMFormatSnapshot?
    var firstHostPTS: CaptureTimestamp?
    var finalHostPTS: CaptureTimestamp?
    let audioFile = "audio.caf"
    let timingFile = "timing.jsonl"
    let clockDomain = "Core Audio tap input acquisition AudioTimeStamp.mHostTime (mach_absolute_time), converted to host nanoseconds"
    let mappingProven = false
}

private struct AudioTimingRecord: Encodable {
    enum CodingKeys: String, CodingKey {
        case event, frameOffset, frameCount, inputTimestamp, callbackTimestamp, hostPTS
        case hostPTSNS = "host_pts_ns"
        case callbackHostNS, gapNS, discontinuity
    }
    let event = "audio_buffer"
    let frameOffset: Int64
    let frameCount: UInt32
    let inputTimestamp: HALTimestamp
    let callbackTimestamp: HALTimestamp
    let hostPTS: CaptureTimestamp
    let hostPTSNS: Int64
    let callbackHostNS: UInt64
    let gapNS: Int64?
    let discontinuity: Bool
}

private struct CaptureIssueRecord: Encodable {
    let event: String
    let message: String
    let callbackHostNS: UInt64
}

func captureHostNanoseconds() -> UInt64 {
    let time = CMClockGetTime(CMClockGetHostTimeClock())
    return UInt64(max(0, CMTimeConvertScale(time, timescale: 1_000_000_000, method: .default).value))
}

func captureNanoseconds(_ time: CMTime) throws -> Int64 {
    guard time.isNumeric, time.epoch == 0, time.timescale > 0 else {
        throw CaptureFailure("The capture stream delivered an invalid presentation timestamp.")
    }
    return CMTimeConvertScale(time, timescale: 1_000_000_000, method: .default).value
}

/// All methods are called on one audio queue after construction and before finalization.
final class AudioRecordingWriter {
    let directory: URL
    private var audioFile: AVAudioFile?
    private var timingFile: FileHandle?
    private var summary: CaptureSummary
    private var expectedHostNS: Int64?
    private var finished = false
    private let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys]
        return encoder
    }()

    init(outputRoot: URL, target: KodiProcess, requestedDuration: Double) throws {
        try FileManager.default.createDirectory(at: outputRoot, withIntermediateDirectories: true)
        let stamp = ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        directory = outputRoot.appendingPathComponent("Kodi-\(stamp)-\(UUID().uuidString)", isDirectory: true)
        guard mkdir(directory.path, 0o700) == 0 else { throw CaptureFailure("Unable to create a new recording folder: \(String(cString: strerror(errno)))") }
        summary = CaptureSummary(status: "preparing", target: target, requestedDuration: requestedDuration,
                                 startedAt: ISO8601DateFormatter().string(from: Date()), startedHostNS: captureHostNanoseconds())
        let timingURL = directory.appendingPathComponent("timing.jsonl")
        try Data().write(to: timingURL, options: .withoutOverwriting)
        timingFile = try FileHandle(forWritingTo: timingURL)
        try saveSummary()
    }

    func markRecordingStarted() throws {
        guard !finished else { throw CaptureFailure("The recording has already ended.") }
        summary.status = "recording"
        summary.captureStartedHostNS = captureHostNanoseconds()
        try saveSummary()
    }

    func append(_ pcm: AVAudioPCMBuffer, timing: CaptureAudioTiming) throws {
        guard !finished else { throw CaptureFailure("Audio arrived after the recording was finalized.") }
        let incoming = PCMFormatSnapshot(pcm.format)
        guard incoming.sampleRate.isFinite, incoming.sampleRate > 0, incoming.channels == 2, pcm.format.commonFormat == .pcmFormatFloat32, pcm.frameLength > 0 else {
            try recordIssue("unsupported_format", message: "Expected a finite native sample rate and stereo Float32 PCM.")
            throw CaptureFailure("The capture stream changed to an unsupported audio format.")
        }
        if let existing = summary.format, existing != incoming {
            try recordIssue("format_changed", message: "PCM layout changed during capture; the recording was stopped.")
            throw CaptureFailure("The capture stream changed PCM layout during recording.")
        }
        let hostPTS = timing.hostPTS
        let hostNS = try captureNanoseconds(hostPTS)
        guard hostNS >= 0 else { throw CaptureFailure("The capture clock could not be converted to host time.") }
        let end = CMTimeAdd(hostPTS, CMTime(seconds: Double(pcm.frameLength) / incoming.sampleRate, preferredTimescale: 1_000_000_000))
        let endNS = try captureNanoseconds(end)
        let gap = expectedHostNS.map { hostNS - $0 }
        let discontinuity = gap.map { abs(Double($0)) > 1_000_000_000 / incoming.sampleRate } ?? false
        if audioFile == nil {
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatLinearPCM,
                AVSampleRateKey: incoming.sampleRate,
                AVNumberOfChannelsKey: incoming.channels,
                AVLinearPCMBitDepthKey: 32,
                AVLinearPCMIsFloatKey: true,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsNonInterleaved: false
            ]
            audioFile = try AVAudioFile(forWriting: directory.appendingPathComponent("audio.caf"), settings: settings,
                                        commonFormat: .pcmFormatFloat32, interleaved: pcm.format.isInterleaved)
            summary.format = incoming
        }
        let record = AudioTimingRecord(frameOffset: summary.totalFrames, frameCount: pcm.frameLength,
                                       inputTimestamp: timing.input, callbackTimestamp: timing.callback, hostPTS: CaptureTimestamp(hostPTS),
                                       hostPTSNS: hostNS, callbackHostNS: timing.callbackHostNS, gapNS: gap, discontinuity: discontinuity)
        var peak = summary.peakMagnitude
        for buffer in UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: pcm.audioBufferList)) {
            guard let bytes = buffer.mData else { throw CaptureFailure("Missing PCM data before writing.") }
            let samples = bytes.assumingMemoryBound(to: Float.self)
            for index in 0..<(Int(buffer.mDataByteSize) / MemoryLayout<Float>.size) {
                guard samples[index].isFinite else { throw CaptureFailure("Core Audio returned a non-finite audio sample.") }
                peak = max(peak, Double(abs(samples[index])))
            }
        }
        try audioFile!.write(from: pcm)
        summary.peakMagnitude = peak
        summary.totalFrames += Int64(pcm.frameLength)
        summary.audioBuffers += 1
        if discontinuity { summary.discontinuities += 1 }
        if summary.firstHostPTS == nil { summary.firstHostPTS = CaptureTimestamp(hostPTS) }
        summary.finalHostPTS = CaptureTimestamp(end)
        expectedHostNS = endNS
        try writeLine(record)
    }

    func recordIssue(_ event: String, message: String) throws {
        try writeLine(CaptureIssueRecord(event: event, message: message, callbackHostNS: captureHostNanoseconds()))
    }

    func finish(reason: String, error: String?) throws -> CaptureSummary {
        if finished { return summary }
        finished = true
        var finalError = error
        if #available(macOS 15.0, *) { audioFile?.close() }
        audioFile = nil
        do {
            try timingFile?.synchronize()
            try timingFile?.close()
            timingFile = nil
            if summary.totalFrames > 0 {
                let written = try AVAudioFile(forReading: directory.appendingPathComponent("audio.caf"))
                if written.length != summary.totalFrames { finalError = finalError ?? "The saved audio frame count does not match the captured frames." }
                if summary.peakMagnitude == 0 { finalError = finalError ?? "Captured audio was entirely silent. Check Kodi playback and audio-only recording permission." }
            } else { finalError = finalError ?? "No audio samples were captured." }
        } catch { finalError = finalError ?? error.localizedDescription }
        summary.status = finalError == nil ? "completed" : "failed"
        summary.error = finalError
        summary.stopReason = reason
        summary.endedAt = ISO8601DateFormatter().string(from: Date())
        summary.endedHostNS = captureHostNanoseconds()
        try saveSummary()
        return summary
    }

    private func saveSummary() throws {
        try encoder.encode(summary).write(to: directory.appendingPathComponent("capture.json"), options: .atomic)
    }

    private func writeLine<T: Encodable>(_ value: T) throws {
        guard let timingFile else { throw CaptureFailure("The timing evidence file is closed.") }
        var data = try encoder.encode(value)
        data.append(0x0A)
        try timingFile.write(contentsOf: data)
    }
}
