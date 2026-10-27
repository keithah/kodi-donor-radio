import Foundation
import Testing
@testable import KodiAudioCaptureFeature

@Test func cancellationDuringNativeSetupDefersCleanupAndNeverPublishesReady() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let harness = try DelayedStartupHarness(root: root)
    harness.run()
    defer { harness.returnFromNative.signal() }
    try #require(harness.enteredNative.wait(timeout: .now() + 3) == .success)

    harness.gate.requestCancellation()
    #expect(harness.finished.wait(timeout: .now() + 0.05) == .timedOut)
    #expect(!harness.snapshot().cleanedUp)
    let waiting = try manifest(at: harness.writer.directory)
    #expect(waiting["status"] as? String == "preparing")
    #expect(waiting["capture_started_host_ns"] == nil)

    harness.returnFromNative.signal()
    try #require(harness.finished.wait(timeout: .now() + 3) == .success)
    let result = harness.snapshot()
    #expect(result.cleanedUp)
    #expect(result.nativeReturned)
    #expect(result.cancelled)
    #expect(result.awakeAssertions == 0)
    #expect(!result.showedRecording)
    #expect(result.error == nil)
    let final = try manifest(at: harness.writer.directory)
    #expect(final["status"] as? String == "failed")
    #expect(final["stop_reason"] as? String == "cancelled")
    #expect(final["capture_started_host_ns"] == nil)
}

@Test func cancellationAfterReadinessStillPreventsLaterActivation() throws {
    let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
    defer { try? FileManager.default.removeItem(at: root) }
    let harness = try DelayedStartupHarness(root: root)
    try harness.gate.start(nativeStart: {}, publishReady: { try harness.writer.markRecordingStarted() })
    #expect(try manifest(at: harness.writer.directory)["status"] as? String == "recording")
    harness.gate.requestCancellation()
    var activated = false
    #expect(throws: CaptureStartupCancelled.self) {
        try harness.gate.performUnlessCancelled { activated = true }
    }
    #expect(!activated)
    _ = try harness.writer.finish(reason: "cancelled", error: CaptureStartupCancelled().localizedDescription)
}

private func manifest(at directory: URL) throws -> [String: Any] {
    let data = try Data(contentsOf: directory.appendingPathComponent("capture.json"))
    return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
}

/// Only the foreign native call is held open. Gate, readiness publication, and manifest finalization are real.
private final class DelayedStartupHarness: @unchecked Sendable {
    struct Result {
        var nativeReturned = false
        var cleanedUp = false
        var cancelled = false
        var awakeAssertions = 0
        var showedRecording = false
        var error: String?
    }
    let gate = CaptureStartupGate()
    let writer: AudioRecordingWriter
    let enteredNative = DispatchSemaphore(value: 0)
    let returnFromNative = DispatchSemaphore(value: 0)
    let finished = DispatchSemaphore(value: 0)
    private let stateLock = NSLock()
    private var result = Result()

    init(root: URL) throws {
        let target = KodiProcess(pid: 42, name: "Kodi", bundlePath: "/tmp/Kodi.app", executablePath: "/tmp/Kodi", launchTime: 123)
        writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: 30)
    }

    func snapshot() -> Result { stateLock.withLock { result } }

    func run() {
        DispatchQueue(label: "KodiAudioCapture.startup-regression").async { [self] in
            var failure: String?
            var reason = "manual"
            do {
                try gate.start(nativeStart: {
                    enteredNative.signal()
                    returnFromNative.wait()
                    stateLock.withLock { result.nativeReturned = true }
                }, publishReady: {
                    try writer.markRecordingStarted()
                })
                try gate.performUnlessCancelled {
                    stateLock.withLock {
                        result.awakeAssertions += 1
                        result.showedRecording = true
                    }
                }
            } catch is CaptureStartupCancelled {
                stateLock.withLock { result.cancelled = true }
                failure = CaptureStartupCancelled().localizedDescription
                reason = "cancelled"
            } catch {
                stateLock.withLock { result.error = error.localizedDescription }
                failure = error.localizedDescription
            }
            // Like transport teardown, cleanup is reached only after the synchronous native call returns.
            stateLock.withLock { result.cleanedUp = true }
            do { _ = try writer.finish(reason: reason, error: failure) }
            catch { stateLock.withLock { result.error = error.localizedDescription } }
            finished.signal()
        }
    }
}
