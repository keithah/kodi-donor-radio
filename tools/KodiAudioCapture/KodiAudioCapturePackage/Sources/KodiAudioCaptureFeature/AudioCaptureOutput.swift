import AVFoundation
import CaptureRing
import CoreAudio
import Foundation

/// Native capture resources and file I/O are confined to `queue`; the HAL callback only fills a bounded C ring.
final class AudioCaptureOutput: @unchecked Sendable {
    let directory: URL
    private let queue = DispatchQueue(label: "org.kodi.donorradio.AudioCapture.writer", qos: .userInitiated)
    private let writer: AudioRecordingWriter
    private let startupGate: CaptureStartupGate
    private let onFailure: @Sendable (String) -> Void
    private var tap = AudioObjectID(kAudioObjectUnknown)
    private var aggregate = AudioObjectID(kAudioObjectUnknown)
    private var ioProc: OpaquePointer?
    private var ring: OpaquePointer?
    private var format: AVAudioFormat?
    private var initialTapFormat: PCMFormatSnapshot?
    private var inputStream = AudioObjectID(kAudioObjectUnknown)
    private var streamFormatListener: AudioObjectPropertyListenerBlock?
    private var drainTimer: DispatchSourceTimer?
    private var formatListener: AudioObjectPropertyListenerBlock?
    private var aliveListener: AudioObjectPropertyListenerBlock?
    private var firstFailure: String?
    private var closed = false
    private var deviceStarted = false

    init(writer: AudioRecordingWriter, startupGate: CaptureStartupGate, onFailure: @escaping @Sendable (String) -> Void) {
        self.writer = writer
        self.startupGate = startupGate
        directory = writer.directory
        self.onFailure = onFailure
    }

    func start(pid: Int32) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            queue.async {
                do {
                    try self.startOnQueue(pid: pid)
                    continuation.resume()
                } catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func startOnQueue(pid: Int32) throws {
        let process = try Self.translatePID(pid)
        tap = try Self.createTap(process: process, pid: pid)
        let uid = try Self.readTapUID(tap)
        initialTapFormat = PCMFormatSnapshot(try Self.readTapFormat(tap))
        aggregate = try Self.createAggregate(tapUID: uid)
        let input = try Self.readAggregateInputFormat(aggregate)
        inputStream = input.stream
        let nativeFormat = input.format
        format = nativeFormat
        let snapshot = PCMFormatSnapshot(nativeFormat)
        guard let createdRing = CaptureRingCreate(32, 16_384, nativeFormat.isInterleaved ? 1 : 2,
                                                  nativeFormat.isInterleaved ? 2 : 1, snapshot.bytesPerFrame) else {
            throw CaptureFailure("Unable to allocate the bounded audio capture buffers.")
        }
        ring = createdRing
        try installListeners()
        ioProc = try Self.attachCallback(device: aggregate, ring: createdRing)
        try startupGate.start(nativeStart: {
            try Self.check(CaptureDeviceCallbackStart(ioProc), "Start audio-only recording; approve macOS audio recording access if requested")
            deviceStarted = true
        }, publishReady: {
            // Cancellation during native start must never become a recording-ready manifest.
            try writer.markRecordingStarted()
        })
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now(), repeating: .milliseconds(10))
        timer.setEventHandler { [weak self] in self?.drain() }
        drainTimer = timer
        timer.resume()
    }

    private func installListeners() throws {
        let formatBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, !self.closed else { return }
            do {
                let current = try Self.readTapFormat(self.tap)
                if let initial = self.initialTapFormat, PCMFormatSnapshot(current) != initial {
                    self.fail("Core Audio changed the tap sample rate or format during recording.", event: "format_changed")
                }
            } catch { self.fail(error.localizedDescription, event: "format_changed") }
        }
        var formatAddress = Self.address(kAudioTapPropertyFormat)
        try Self.check(AudioObjectAddPropertyListenerBlock(tap, &formatAddress, queue, formatBlock), "Watch the audio tap format")
        formatListener = formatBlock
        let streamBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, !self.closed else { return }
            do {
                let current = try Self.readStreamFormat(self.inputStream)
                if let format = self.format, PCMFormatSnapshot(current) != PCMFormatSnapshot(format) {
                    self.fail("Core Audio changed the aggregate input sample rate or format during recording.", event: "format_changed")
                }
            } catch { self.fail(error.localizedDescription, event: "format_changed") }
        }
        var streamAddress = Self.address(kAudioStreamPropertyVirtualFormat)
        try Self.check(AudioObjectAddPropertyListenerBlock(inputStream, &streamAddress, queue, streamBlock), "Watch the aggregate input format")
        streamFormatListener = streamBlock
        let aliveBlock: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self, !self.closed else { return }
            do {
                if try Self.readUInt32(self.aggregate, selector: kAudioDevicePropertyDeviceIsAlive) == 0 {
                    self.fail("The audio capture device disconnected.", event: "device_disconnected")
                }
            } catch { self.fail(error.localizedDescription, event: "device_disconnected") }
        }
        var aliveAddress = Self.address(kAudioDevicePropertyDeviceIsAlive)
        try Self.check(AudioObjectAddPropertyListenerBlock(aggregate, &aliveAddress, queue, aliveBlock), "Watch the audio capture device")
        aliveListener = aliveBlock
    }

    private func drain() {
        guard firstFailure == nil, let ring, let format else { return }
        for _ in 0..<32 {
            guard let slot = CaptureRingPeek(ring) else { break }
            do {
                let timing = try CaptureAudioTiming(inputTime: slot.pointee.inputTime, callbackTime: slot.pointee.callbackTime,
                                                    arrivalHostTime: slot.pointee.arrivalHostTime)
                let pcm = try PCMBufferCopy.copy(format: format, audioBufferList: slot.pointee.buffers,
                                                frameCount: Int(slot.pointee.frameCount))
                CaptureRingConsume(ring)
                try writer.append(pcm, timing: timing)
            } catch {
                fail(error.localizedDescription)
                break
            }
        }
        switch Int(CaptureRingGetFailure(ring)) {
        case CaptureRingFailureOverflow: fail("The bounded audio buffer pool overflowed. Recording is incomplete; no dropped audio is hidden.", event: "buffer_overflow")
        case CaptureRingFailureLayout: fail("Core Audio supplied an oversized buffer or changed its PCM layout.", event: "format_changed")
        case CaptureRingFailureTimestamp: fail("Core Audio supplied an invalid input timestamp.", event: "invalid_timestamp")
        default: break
        }
    }

    func finish(reason: String, error: String?) async throws -> CaptureSummary {
        try await withCheckedThrowingContinuation { continuation in
            queue.async {
                self.closed = true
                self.drainTimer?.cancel()
                self.drainTimer = nil
                let cleanupErrors = self.cleanup()
                let failure = self.firstFailure ?? error ?? (cleanupErrors.isEmpty ? nil : cleanupErrors.joined(separator: "; "))
                do { continuation.resume(returning: try self.writer.finish(reason: reason, error: failure)) }
                catch { continuation.resume(throwing: error) }
            }
        }
    }

    private func cleanup() -> [String] {
        var errors: [String] = []
        func collect(_ result: OSStatus, _ operation: String) -> Bool {
            if result != noErr { errors.append("\(operation) failed (\(result)).") }
            return result == noErr
        }
        var callbackStopped = !deviceStarted
        var retainedCallback: OpaquePointer?
        if aggregate != kAudioObjectUnknown, let ioProc {
            if deviceStarted { callbackStopped = collect(CaptureDeviceCallbackStop(ioProc), "Stop audio device") }
            let removed = collect(CaptureDeviceCallbackDestroy(ioProc), "Remove audio callback")
            callbackStopped = removed || callbackStopped
            if !removed { retainedCallback = ioProc }
        }
        ioProc = nil
        deviceStarted = false
        drain()
        if let formatListener, tap != kAudioObjectUnknown {
            var address = Self.address(kAudioTapPropertyFormat)
            _ = collect(AudioObjectRemovePropertyListenerBlock(tap, &address, queue, formatListener), "Remove format observer")
        }
        if let aliveListener, aggregate != kAudioObjectUnknown {
            var address = Self.address(kAudioDevicePropertyDeviceIsAlive)
            _ = collect(AudioObjectRemovePropertyListenerBlock(aggregate, &address, queue, aliveListener), "Remove device observer")
        }
        if let streamFormatListener, inputStream != kAudioObjectUnknown {
            var address = Self.address(kAudioStreamPropertyVirtualFormat)
            _ = collect(AudioObjectRemovePropertyListenerBlock(inputStream, &address, queue, streamFormatListener), "Remove input format observer")
        }
        formatListener = nil
        streamFormatListener = nil
        aliveListener = nil
        if aggregate != kAudioObjectUnknown {
            callbackStopped = collect(AudioHardwareDestroyAggregateDevice(aggregate), "Remove private capture device") || callbackStopped
            aggregate = AudioObjectID(kAudioObjectUnknown)
        }
        if tap != kAudioObjectUnknown {
            _ = collect(AudioHardwareDestroyProcessTap(tap), "Remove Kodi audio tap")
            tap = AudioObjectID(kAudioObjectUnknown)
        }
        if callbackStopped, let retainedCallback { CaptureDeviceCallbackForget(retainedCallback) }
        if callbackStopped, let ring { CaptureRingDestroy(ring) }
        // In the exceptional case where every HAL stop/destroy call fails, retain memory rather than free a live callback's storage.
        ring = nil
        for message in errors { try? writer.recordIssue("cleanup_error", message: message) }
        return errors
    }

    private func fail(_ message: String, event: String = "capture_error") {
        guard firstFailure == nil else { return }
        firstFailure = message
        try? writer.recordIssue(event, message: message)
        if !closed { onFailure(message) }
    }

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    }

    private static func check(_ status: OSStatus, _ operation: String) throws {
        guard status == noErr else { throw CaptureFailure("\(operation) failed (Core Audio \(status)).") }
    }

    private static func readUInt32(_ object: AudioObjectID, selector: AudioObjectPropertySelector) throws -> UInt32 {
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        var property = address(selector)
        try check(AudioObjectGetPropertyData(object, &property, 0, nil, &size, &value), "Read audio process/device state")
        return value
    }

    private static func readTapUID(_ tap: AudioObjectID) throws -> String {
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var property = address(kAudioTapPropertyUID)
        try check(AudioObjectGetPropertyData(tap, &property, 0, nil, &size, &value), "Read audio tap identifier")
        guard let value else { throw CaptureFailure("Core Audio returned an empty tap identifier.") }
        return value.takeRetainedValue() as String
    }

    private static func translatePID(_ pid: Int32) throws -> AudioObjectID {
        var translatedPID = pid
        var process = AudioObjectID(kAudioObjectUnknown)
        var property = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        try check(AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &property,
                                            UInt32(MemoryLayout<Int32>.size), &translatedPID, &size, &process), "Find Kodi's audio process")
        guard process != kAudioObjectUnknown else { throw CaptureFailure("Core Audio cannot find the selected Kodi audio process. Start Kodi playback and retry.") }
        guard try readUInt32(process, selector: kAudioProcessPropertyPID) == UInt32(pid) else {
            throw CaptureFailure("Kodi's Core Audio process identity changed before capture.")
        }
        return process
    }

    private static func createTap(process: AudioObjectID, pid: Int32) throws -> AudioObjectID {
        let description = CATapDescription(stereoMixdownOfProcesses: [process])
        description.name = "Kodi Audio Capture PID \(pid)"
        description.isPrivate = true
        description.isExclusive = false
        description.muteBehavior = .unmuted
        if #available(macOS 26.0, *) { description.isProcessRestoreEnabled = false }
        var identifier = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateProcessTap(description, &identifier), "Create the selected Kodi audio tap")
        return identifier
    }

    private static func createAggregate(tapUID: String) throws -> AudioObjectID {
        let composition: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Kodi Audio Capture \(UUID().uuidString)",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceTapAutoStartKey: false,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: tapUID, kAudioSubTapDriftCompensationKey: true]]
        ]
        var identifier = AudioObjectID(kAudioObjectUnknown)
        try check(AudioHardwareCreateAggregateDevice(composition as CFDictionary, &identifier), "Create the private audio capture device")
        return identifier
    }

    private static func attachCallback(device: AudioObjectID, ring: OpaquePointer) throws -> OpaquePointer {
        var callback: OpaquePointer?
        try check(CaptureDeviceCallbackCreate(device, ring, &callback), "Attach the bounded audio capture callback")
        guard let callback else { throw CaptureFailure("Core Audio did not create its capture callback.") }
        return callback
    }

    private static func readAggregateInputFormat(_ device: AudioObjectID) throws -> (stream: AudioObjectID, format: AVAudioFormat) {
        var property = address(kAudioDevicePropertyStreams)
        property.mScope = kAudioObjectPropertyScopeInput
        var size: UInt32 = 0
        try check(AudioObjectGetPropertyDataSize(device, &property, 0, nil, &size), "Read aggregate input streams")
        guard size == MemoryLayout<AudioObjectID>.size else { throw CaptureFailure("The private capture device did not expose exactly one stereo input stream.") }
        var stream = AudioObjectID(kAudioObjectUnknown)
        try check(AudioObjectGetPropertyData(device, &property, 0, nil, &size, &stream), "Read aggregate input stream identity")
        return (stream, try readStreamFormat(stream))
    }

    private static func readStreamFormat(_ stream: AudioObjectID) throws -> AVAudioFormat {
        try readFormat(stream, selector: kAudioStreamPropertyVirtualFormat)
    }

    private static func readTapFormat(_ tap: AudioObjectID) throws -> AVAudioFormat {
        try readFormat(tap, selector: kAudioTapPropertyFormat)
    }

    private static func readFormat(_ object: AudioObjectID, selector: AudioObjectPropertySelector) throws -> AVAudioFormat {
        var description = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var property = address(selector)
        try check(AudioObjectGetPropertyData(object, &property, 0, nil, &size, &description), "Read native audio format")
        guard description.mFormatID == kAudioFormatLinearPCM, description.mChannelsPerFrame == 2,
              description.mBitsPerChannel == 32, description.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              description.mFormatFlags & kAudioFormatFlagIsBigEndian == 0,
              description.mSampleRate.isFinite, description.mSampleRate > 0,
              let format = AVAudioFormat(streamDescription: &description), format.commonFormat == .pcmFormatFloat32 else {
            throw CaptureFailure("The selected Kodi tap did not provide supported native-rate stereo Float32 PCM.")
        }
        return format
    }
}
