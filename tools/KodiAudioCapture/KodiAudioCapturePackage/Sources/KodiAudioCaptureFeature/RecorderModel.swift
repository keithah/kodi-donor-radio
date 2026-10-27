import AppKit
import AVFoundation
import Combine
import CoreAudio
import Darwin

@MainActor
public final class RecorderModel: NSObject, ObservableObject, AVAudioPlayerDelegate {
    @Published var processes: [KodiProcess] = []
    @Published var selectedPID: Int32?
    @Published var duration = 30.0
    @Published var outputRoot = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Movies/KodiAudioCapture", isDirectory: true)
    @Published private(set) var status = "Choose a running Kodi build to record."
    @Published private(set) var errorMessage: String?
    @Published private(set) var isActive = false
    @Published private(set) var isStopping = false
    @Published private(set) var isStarting = false
    @Published private(set) var recordingDirectory: URL?
    @Published private(set) var elapsed = 0.0
    @Published private(set) var capturedFrames: Int64 = 0
    @Published private(set) var isPlaying = false

    private var options: CaptureOptions?
    private let terminateAutomation: @MainActor (Int32) -> Void
    private let automationExitRequested: Bool
    private var prepared = false
    private var output: AudioCaptureOutput?
    private var wakeAssertions: CaptureWakeAssertions?
    private var startTask: Task<Void, Never>?
    private var startupGate: CaptureStartupGate?
    private var endTask: Task<Void, Never>?
    private var durationTask: Task<Void, Never>?
    private var stopRequested = false
    private var player: AVAudioPlayer?

    public init(arguments: [String] = Array(ProcessInfo.processInfo.arguments.dropFirst()),
                terminateAutomation: @escaping @MainActor (Int32) -> Void = { Darwin.exit($0) }) {
        self.terminateAutomation = terminateAutomation
        automationExitRequested = arguments.contains("--auto-record") && arguments.contains("--exit-after-recording")
        super.init()
        do {
            let parsed = try CaptureOptions(arguments: arguments)
            options = parsed
            selectedPID = parsed.pid
            duration = parsed.duration
            outputRoot = parsed.outputRoot
        } catch {
            errorMessage = error.localizedDescription
            status = "Launch options need attention."
        }
    }

    func prepare() {
        guard !prepared else { return }
        prepared = true
        guard options != nil else {
            exitIfRequested(failed: true)
            return
        }
        refreshProcesses()
        if options?.autoRecord == true { beginRecording() }
    }

    func refreshProcesses() {
        processes = NSWorkspace.shared.runningApplications.compactMap(Self.identity).sorted { $0.pid < $1.pid }
        if selectedPID == nil, processes.count == 1 { selectedPID = processes[0].pid }
    }

    static func identity(_ application: NSRunningApplication) -> KodiProcess? {
        guard !application.isTerminated, application.bundleIdentifier == "org.xbmc.kodi",
              let executable = application.executableURL, let bundle = application.bundleURL,
              let launchDate = application.launchDate else { return nil }
        return KodiProcess(pid: application.processIdentifier, name: application.localizedName ?? "Kodi",
                           bundlePath: bundle.path, executablePath: executable.path,
                           launchTime: launchDate.timeIntervalSince1970)
    }

    private func validate(_ expected: KodiProcess) throws {
        guard let application = NSRunningApplication(processIdentifier: expected.pid),
              let actual = Self.identity(application), actual == expected else {
            throw CaptureFailure("The selected Kodi process exited or changed. Refresh the list and choose it again.")
        }
    }

    func beginRecording() {
        guard !isActive, options != nil else { return }
        guard duration.isFinite, (1...1200).contains(duration) else {
            errorMessage = "Choose a duration between 1 and 1200 seconds."
            exitIfRequested(failed: true)
            return
        }
        guard let target = processes.first(where: { $0.pid == selectedPID }) else {
            errorMessage = "The selected Kodi process is not running. Refresh the list and select its PID."
            status = "Recording could not start."
            exitIfRequested(failed: true)
            return
        }
        stopPlayback()
        isActive = true
        isStarting = true
        isStopping = false
        stopRequested = false
        endTask = nil
        capturedFrames = 0
        elapsed = 0
        errorMessage = nil
        recordingDirectory = nil
        status = "Preparing Kodi audio capture…"
        let seconds = duration
        let root = outputRoot
        let gate = CaptureStartupGate()
        startupGate = gate
        startTask = Task { [weak self] in
            await self?.startSession(target: target, seconds: seconds, root: root, gate: gate)
        }
    }

    private func startSession(target: KodiProcess, seconds: Double, root: URL, gate: CaptureStartupGate) async {
        do {
            try validate(target)
            let writer = try AudioRecordingWriter(outputRoot: root, target: target, requestedDuration: seconds)
            let sink = AudioCaptureOutput(writer: writer, startupGate: gate) { [weak self] message in
                Task { @MainActor in await self?.stop(reason: "capture_error", error: message) }
            }
            output = sink
            recordingDirectory = writer.directory
            guard !stopRequested else { throw CaptureStartupCancelled() }
            status = "Waiting for macOS audio setup… Approve the audio-only recording request if one appears."
            try await sink.start(pid: target.pid)
            try gate.performUnlessCancelled {
                guard !stopRequested else { throw CaptureStartupCancelled() }
                wakeAssertions = try CaptureWakeAssertions()
                try validate(target)
                isStarting = false
                status = "Recording Kodi audio…"
            }
            if !stopRequested {
                let started = captureHostNanoseconds()
                durationTask = Task { [weak self] in
                    while !Task.isCancelled {
                        try? await Task.sleep(for: .milliseconds(200))
                        guard !Task.isCancelled, let self else { return }
                        self.elapsed = Double(captureHostNanoseconds() - started) / 1_000_000_000
                        do { try self.validate(target) } catch {
                            await self.stop(reason: "target_exited", error: error.localizedDescription)
                            return
                        }
                        if self.elapsed >= seconds {
                            await self.stop(reason: "duration")
                            return
                        }
                    }
                }
            }
        } catch {
            let reason = error is CaptureStartupCancelled ? "cancelled" : "start_failed"
            await finalize(reason: reason, error: Self.captureErrorDescription(error))
        }
    }

    public func stop(reason: String = "manual", error: String? = nil) async {
        guard isActive else { return }
        startupGate?.requestCancellation()
        stopRequested = true
        if let endTask { await endTask.value; return }
        isStopping = true
        status = isStarting
            ? "Cancellation requested. Waiting for macOS audio setup to return…"
            : "Finishing recording…"
        let task = Task { [weak self] in
            guard let self else { return }
            await self.startTask?.value
            if self.isActive { await self.finalize(reason: reason, error: error) }
        }
        endTask = task
        await task.value
    }

    private func finalize(reason: String, error: String?) async {
        durationTask?.cancel()
        durationTask = nil
        var finalError = error
        if let output {
            do {
                let result = try await output.finish(reason: reason, error: finalError)
                capturedFrames = result.totalFrames
                finalError = result.error
            } catch { finalError = finalError ?? error.localizedDescription }
        }
        output = nil
        wakeAssertions?.release()
        wakeAssertions = nil
        errorMessage = finalError
        status = finalError == nil ? "Recording saved · \(capturedFrames.formatted()) audio frames." : "Recording failed. Evidence saved when available."
        isActive = false
        isStarting = false
        isStopping = false
        startupGate = nil
        exitIfRequested(failed: finalError != nil)
    }

    private func exitIfRequested(failed: Bool) {
        if automationExitRequested {
            // This runs before capture starts or after capture finalization and resource cleanup.
            terminateAutomation(failed ? EXIT_FAILURE : EXIT_SUCCESS)
        }
    }

    static func captureErrorDescription(_ error: any Error) -> String {
        error.localizedDescription
    }

    func openRecordingAccessSettings() {
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture") {
            NSWorkspace.shared.open(url)
        }
    }

    func chooseOutputFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = outputRoot
        if panel.runModal() == .OK, let url = panel.url { outputRoot = url }
    }

    func openRecording() {
        if let recordingDirectory { NSWorkspace.shared.open(recordingDirectory) }
    }

    func togglePlayback() {
        if isPlaying { stopPlayback(); return }
        guard let recordingDirectory else { return }
        do {
            player = try AVAudioPlayer(contentsOf: recordingDirectory.appendingPathComponent("audio.caf"))
            player?.delegate = self
            guard player?.play() == true else { throw CaptureFailure("The saved recording could not start playing.") }
            isPlaying = true
        } catch { errorMessage = error.localizedDescription }
    }

    public nonisolated func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        let finishedPlayer = ObjectIdentifier(player)
        Task { @MainActor [weak self] in
            guard let self, self.player.map(ObjectIdentifier.init) == finishedPlayer else { return }
            self.stopPlayback()
            if !flag { self.errorMessage = "Playback ended before the saved recording finished." }
        }
    }

    public nonisolated func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: (any Error)?) {
        let finishedPlayer = ObjectIdentifier(player)
        let message = error?.localizedDescription ?? "The saved recording could not be decoded."
        Task { @MainActor [weak self] in
            guard let self, self.player.map(ObjectIdentifier.init) == finishedPlayer else { return }
            self.stopPlayback()
            self.errorMessage = message
        }
    }

    func stopPlayback() {
        player?.delegate = nil
        player?.stop()
        player = nil
        isPlaying = false
    }
}

@MainActor
public final class CaptureApplicationDelegate: NSObject, NSApplicationDelegate {
    public var recorder: RecorderModel?

    public func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let recorder, recorder.isActive else { return .terminateNow }
        Task {
            await recorder.stop(reason: "application_exit")
            sender.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    public func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
