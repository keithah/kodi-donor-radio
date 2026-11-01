import SwiftUI
import KodiAudioCaptureFeature

@main
struct KodiAudioCaptureApp: App {
    @StateObject private var recorder = RecorderModel()
    @NSApplicationDelegateAdaptor(CaptureApplicationDelegate.self) private var applicationDelegate

    var body: some Scene {
        WindowGroup {
            ContentView(recorder: recorder)
                .onAppear { applicationDelegate.recorder = recorder }
        }
        .windowResizability(.contentSize)
    }
}
