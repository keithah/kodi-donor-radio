import SwiftUI

public struct ContentView: View {
    @ObservedObject private var recorder: RecorderModel

    public init(recorder: RecorderModel) { self.recorder = recorder }

    public var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(alignment: .top) {
                Image(systemName: "waveform.circle.fill")
                    .font(.system(size: 42)).foregroundStyle(.blue)
                VStack(alignment: .leading, spacing: 5) {
                    Text("Kodi Audio Capture").font(.title2.bold())
                    Text("Record a local Kodi playback test with timing evidence.")
                        .foregroundStyle(.secondary)
                }
            }
            GroupBox("Kodi build") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        Picker("Process", selection: $recorder.selectedPID) {
                            Text("Choose Kodi…").tag(Int32?.none)
                            ForEach(recorder.processes) { process in
                                Text("\(process.name) · PID \(process.pid) · \(process.bundlePath)").tag(Optional(process.pid))
                            }
                        }
                        Button("Refresh") { recorder.refreshProcesses() }
                    }
                    if let selected = recorder.processes.first(where: { $0.pid == recorder.selectedPID }) {
                        Text(selected.executablePath).font(.caption.monospaced()).textSelection(.enabled)
                    } else {
                        Text("Start the Kodi build you want to test, then refresh this list.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }.padding(6)
            }.disabled(recorder.isActive)
            GroupBox("Recording") {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("Duration")
                        TextField("Seconds", value: $recorder.duration, format: .number)
                            .frame(width: 90).textFieldStyle(.roundedBorder)
                        Text("seconds · 1–1200").foregroundStyle(.secondary)
                        Spacer()
                    }
                    HStack {
                        Text(recorder.outputRoot.path).font(.caption).textSelection(.enabled)
                        Spacer()
                        Button("Choose folder…") { recorder.chooseOutputFolder() }
                    }
                    Text("Audio only · Native-rate lossless stereo · microphone off")
                        .font(.caption).foregroundStyle(.secondary)
                }.padding(6)
            }.disabled(recorder.isActive)
            VStack(alignment: .leading, spacing: 8) {
                Text(recorder.status).font(.headline).accessibilityIdentifier("recordingStatus")
                if recorder.isActive {
                    ProgressView(value: min(recorder.elapsed, recorder.duration), total: max(1, recorder.duration))
                    Text("\(recorder.elapsed, specifier: "%.1f") seconds").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                if let message = recorder.errorMessage {
                    Text(message).foregroundStyle(.red).textSelection(.enabled)
                }
                if let directory = recorder.recordingDirectory {
                    Text(directory.path).font(.caption.monospaced()).textSelection(.enabled)
                }
            }
            HStack {
                if recorder.isActive {
                    Button("Stop recording") { Task { await recorder.stop() } }
                        .disabled(recorder.isStopping)
                } else {
                    Button("Start recording") { recorder.beginRecording() }
                        .buttonStyle(.borderedProminent)
                        .disabled(recorder.selectedPID == nil)
                        .accessibilityIdentifier("startRecording")
                }
                Spacer()
                Button(recorder.isPlaying ? "Stop playback" : "Play recording") { recorder.togglePlayback() }
                    .disabled(recorder.isActive || recorder.capturedFrames == 0)
                Button("Open recording folder") { recorder.openRecording() }
                    .disabled(recorder.recordingDirectory == nil)
            }
            Button("Recording access settings…") { recorder.openRecordingAccessSettings() }
                .disabled(recorder.isActive)
            Text("The Mac stays awake only during recording. macOS may request audio-only recording permission and require an app restart. Recordings preserve timing gaps; playback-clock mapping remains unproven.")
                .font(.caption).foregroundStyle(.secondary)
        }
        .padding(24)
        .frame(minWidth: 660, minHeight: 540)
        .task { recorder.prepare() }
    }
}
