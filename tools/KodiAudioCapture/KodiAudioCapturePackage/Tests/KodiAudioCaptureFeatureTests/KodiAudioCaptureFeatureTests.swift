import AVFoundation
import CoreMedia
import CoreAudio
import CaptureRing
import Testing
@testable import KodiAudioCaptureFeature

@Test func launchOptionsEnforceSafeBoundsAndExplicitTarget() throws {
    let options = try CaptureOptions(arguments: ["--pid", "42", "--duration", "20.5", "--output-root", "/tmp/capture-test", "--auto-record", "--exit-after-recording"])
    #expect(options.pid == 42)
    #expect(options.duration == 20.5)
    #expect(options.outputRoot.path == "/tmp/capture-test")
    #expect(options.autoRecord && options.exitAfterRecording)
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--duration", "0"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--duration", "1201"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--duration", "nan"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--pid", "-1"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--auto-record"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--unknown"]) }
    #expect(throws: (any Error).self) { try CaptureOptions(arguments: ["--pid"]) }
    #expect(try CaptureOptions(arguments: []).duration == 30)
}

@Test func launchOptionsIgnoreCocoaDefaultArguments() throws {
    let options = try CaptureOptions(arguments: ["-AppleLanguages", "(en)", "-NSDocumentRevisionsDebugMode", "YES"])
    #expect(options.duration == 30)
}

@Test func pcmCopyOwnsSamplesAndRejectsWrongFrameCount() throws {
    let source = try #require(AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!, frameCapacity: 3))
    source.frameLength = 3
    source.floatChannelData![0].update(from: [0.25, -0.5, 0.75], count: 3)
    source.floatChannelData![1].update(from: [-0.25, 0.5, -0.75], count: 3)
    let copied = try PCMBufferCopy.copy(format: source.format, audioBufferList: source.audioBufferList, frameCount: 3)
    source.floatChannelData![0][0] = 1
    #expect(copied.frameLength == 3)
    #expect(Array(UnsafeBufferPointer(start: copied.floatChannelData![0], count: 3)) == [0.25, -0.5, 0.75])
    #expect(Array(UnsafeBufferPointer(start: copied.floatChannelData![1], count: 3)) == [-0.25, 0.5, -0.75])
    #expect(throws: (any Error).self) { try PCMBufferCopy.copy(format: source.format, audioBufferList: source.audioBufferList, frameCount: 4) }
}

@Test func pcmCopySupportsInterleavedStereo() throws {
    let format = try #require(AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: true))
    let source = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 2))
    source.frameLength = 2
    source.floatChannelData![0].update(from: [0.1, 0.2, -0.1, -0.2], count: 4)
    let copied = try PCMBufferCopy.copy(format: format, audioBufferList: source.audioBufferList, frameCount: 2)
    #expect(Array(UnsafeBufferPointer(start: copied.floatChannelData![0], count: 4)) == [0.1, 0.2, -0.1, -0.2])
}

@Test func recordingWriterCountsFramesKeepsGapsAndFinalizesLosslessFile() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi.app/Contents/MacOS/Kodi", launchTime: 123)
    let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 480))
    pcm.frameLength = 480
    for channel in 0..<2 { pcm.floatChannelData![channel].initialize(repeating: 0.25, count: 480) }
    try writer.append(pcm, timing: testTiming(hostNS: 1_000_000_000))
    try writer.append(pcm, timing: testTiming(hostNS: 1_030_000_000))
    let summary = try writer.finish(reason: "duration", error: nil)
    #expect(summary.status == "completed")
    #expect(summary.totalFrames == 960)
    #expect(summary.discontinuities == 1)
    #expect(summary.mappingProven == false)
    let file = try AVAudioFile(forReading: writer.directory.appendingPathComponent("audio.caf"))
    #expect(file.length == 960)
    let decoded = try #require(AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 960))
    try file.read(into: decoded)
    #expect(decoded.floatChannelData![0][479] == 0.25)
    let entries = try String(contentsOf: writer.directory.appendingPathComponent("timing.jsonl"), encoding: .utf8).split(separator: "\n")
    #expect(entries.count == 2)
    let second = try jsonDictionary(Data(entries[1].utf8))
    #expect(second["frame_offset"] as? Int == 480)
    #expect(second["host_pts_ns"] as? Int64 == 1_030_000_000)
    #expect(second["host_ptsns"] == nil)
    #expect(second["gap_ns"] as? Int == 20_000_000)
}

@Test func emptyAndInterruptedRecordingsNeverReportSuccess() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
    let empty = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let emptySummary = try empty.finish(reason: "duration", error: nil)
    #expect(emptySummary.status == "failed")
    #expect(emptySummary.error?.contains("No audio") == true)
    let interrupted = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    #expect(interrupted.directory != empty.directory)
    let failed = try interrupted.finish(reason: "capture_error", error: "Permission revoked")
    #expect(failed.status == "failed")
    #expect(failed.error == "Permission revoked")
}

@Test func readinessIsPublishedOnlyWhenStreamIsArmed() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
    let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let summaryURL = writer.directory.appendingPathComponent("capture.json")
    let preparing = try jsonDictionary(Data(contentsOf: summaryURL))
    #expect(preparing["status"] as? String == "preparing")
    #expect(preparing["capture_started_host_ns"] == nil)
    try writer.markRecordingStarted()
    let recording = try jsonDictionary(Data(contentsOf: summaryURL))
    #expect(recording["status"] as? String == "recording")
    #expect((recording["capture_started_host_ns"] as? UInt64 ?? 0) > 0)
    #expect(recording["mapping_proven"] as? Bool == false)
    _ = try writer.finish(reason: "manual", error: nil)
    #expect(throws: (any Error).self) { try writer.markRecordingStarted() }
}

@Test func changedAudioLayoutStopsAndRetainsExplicitFailure() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
    let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let planar = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
    let packed = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 2, interleaved: true)!
    let first = try #require(AVAudioPCMBuffer(pcmFormat: planar, frameCapacity: 1))
    first.frameLength = 1
    first.floatChannelData![0][0] = 0
    first.floatChannelData![1][0] = 0
    let changed = try #require(AVAudioPCMBuffer(pcmFormat: packed, frameCapacity: 1))
    changed.frameLength = 1
    changed.floatChannelData![0][0] = 0
    changed.floatChannelData![0][1] = 0
    try writer.append(first, timing: testTiming(hostNS: 1_000_000_000))
    #expect(throws: (any Error).self) { try writer.append(changed, timing: testTiming(hostNS: 1_000_000_000)) }
    let result = try writer.finish(reason: "capture_error", error: "Audio layout changed")
    #expect(result.status == "failed")
    #expect(result.totalFrames == 1)
    let lines = try String(contentsOf: writer.directory.appendingPathComponent("timing.jsonl"), encoding: .utf8).split(separator: "\n")
    let last = try jsonDictionary(Data(try #require(lines.last).utf8))
    #expect(last["event"] as? String == "format_changed")
}

private func testTiming(hostNS: Int64) throws -> CaptureAudioTiming {
    var input = AudioTimeStamp()
    input.mHostTime = CMClockConvertHostTimeToSystemUnits(CMTime(value: hostNS, timescale: 1_000_000_000))
    input.mSampleTime = 123.5
    input.mFlags = [.hostTimeValid, .sampleTimeValid]
    return try CaptureAudioTiming(inputTime: input, callbackTime: input, arrivalHostTime: input.mHostTime)
}

@Test func boundedRingFailsOnOverflowInsteadOfDroppingAndContinuing() throws {
    let ring = try #require(CaptureRingCreate(1, 4, 2, 1, 4))
    defer { CaptureRingDestroy(ring) }
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 2)!, frameCapacity: 4))
    pcm.frameLength = 4
    pcm.floatChannelData![0].initialize(repeating: 0.25, count: 4)
    pcm.floatChannelData![1].initialize(repeating: -0.25, count: 4)
    var timestamp = AudioTimeStamp()
    timestamp.mHostTime = 7
    timestamp.mFlags = [.hostTimeValid]
    #expect(CaptureRingPush(ring, pcm.audioBufferList, &timestamp, &timestamp, 9))
    pcm.floatChannelData![0][0] = 1
    let slot = try #require(CaptureRingPeek(ring))
    #expect(slot.pointee.frameCount == 4)
    #expect(slot.pointee.buffers.pointee.mBuffers.mData!.assumingMemoryBound(to: Float.self)[0] == 0.25)
    #expect(!CaptureRingPush(ring, pcm.audioBufferList, &timestamp, &timestamp, 10))
    #expect(CaptureRingGetFailure(ring) == CaptureRingFailureOverflow)
    CaptureRingConsume(ring)
    #expect(CaptureRingPeek(ring) == nil)
    #expect(!CaptureRingPush(ring, pcm.audioBufferList, &timestamp, &timestamp, 11))
}

@Test func nativeRateAndFractionalSampleTimestampArePreserved() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
    let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 44_100, channels: 2)!, frameCapacity: 441))
    pcm.frameLength = 441
    for channel in 0..<2 { pcm.floatChannelData![channel].initialize(repeating: 0.125, count: 441) }
    try writer.append(pcm, timing: testTiming(hostNS: 1_000_000_000))
    try writer.append(pcm, timing: testTiming(hostNS: 1_010_000_000))
    let summary = try writer.finish(reason: "duration", error: nil)
    #expect(summary.status == "completed")
    #expect(summary.format?.sampleRate == 44_100)
    #expect(summary.totalFrames == 882)
    #expect(summary.discontinuities == 0)
    let file = try AVAudioFile(forReading: writer.directory.appendingPathComponent("audio.caf"))
    #expect(file.fileFormat.sampleRate == 44_100)
    let lines = try String(contentsOf: writer.directory.appendingPathComponent("timing.jsonl"), encoding: .utf8).split(separator: "\n")
    let record = try jsonDictionary(Data(try #require(lines.first).utf8))
    let input = try #require(record["input_timestamp"] as? [String: Any])
    #expect(input["sample_time"] as? Double == 123.5)
    #expect(input["flags"] as? UInt32 == AudioTimeStampFlags([.hostTimeValid, .sampleTimeValid]).rawValue)
}

@Test func invalidInputClockCannotBecomeAnAbsoluteAnchor() {
    let timestamp = AudioTimeStamp()
    #expect(throws: (any Error).self) { try CaptureAudioTiming(inputTime: timestamp, callbackTime: timestamp, arrivalHostTime: 1) }
}

@Test func silentTapDoesNotMasqueradeAsSuccessfulPermissionOrPlayback() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
    let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    let pcm = try #require(AVAudioPCMBuffer(pcmFormat: AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!, frameCapacity: 480))
    pcm.frameLength = 480
    for channel in 0..<2 { pcm.floatChannelData![channel].initialize(repeating: 0, count: 480) }
    try writer.append(pcm, timing: testTiming(hostNS: 1_000_000_000))
    let result = try writer.finish(reason: "duration", error: nil)
    #expect(result.totalFrames == 480)
    #expect(result.status == "failed")
    #expect(result.error?.contains("silent") == true)
    let evidence = try AVAudioFile(forReading: writer.directory.appendingPathComponent("audio.caf"))
    #expect(evidence.length == 480)
}

private func jsonDictionary(_ data: Data) throws -> [String: Any] {
    try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
}

@Test @MainActor func invalidTargetAutomationFailsWithoutAnOutputManifest() {
    var exitCodes: [Int32] = []
    let model = RecorderModel(arguments: ["--pid", "42", "--auto-record", "--exit-after-recording"],
                              terminateAutomation: { exitCodes.append($0) })
    model.processes = []
    model.beginRecording()
    #expect(exitCodes == [1])
    #expect(model.errorMessage != nil)
    #expect(model.recordingDirectory == nil)
    #expect(!model.isActive)
}

@Test @MainActor func missingPIDAutomationFailsWithoutStartingCapture() {
    var exitCodes: [Int32] = []
    let model = RecorderModel(arguments: ["--auto-record", "--exit-after-recording"],
                              terminateAutomation: { exitCodes.append($0) })
    model.prepare()
    #expect(exitCodes == [1])
    #expect(model.errorMessage != nil)
    #expect(model.recordingDirectory == nil)
    #expect(!model.isActive)
}

@Test @MainActor func invalidTargetKeepsManualErrorViewOpen() {
    var exitCodes: [Int32] = []
    let model = RecorderModel(arguments: ["--pid", "42"], terminateAutomation: { exitCodes.append($0) })
    model.processes = []
    model.beginRecording()
    #expect(exitCodes.isEmpty)
    #expect(model.errorMessage != nil)
    #expect(!model.isActive)
}
