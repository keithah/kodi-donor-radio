// swift-tools-version: 6.1
import PackageDescription

let package = Package(
    name: "KodiAudioCaptureFeature",
    platforms: [.macOS("14.2")],
    products: [.library(name: "KodiAudioCaptureFeature", targets: ["KodiAudioCaptureFeature"])],
    targets: [
        .target(name: "CaptureRing", publicHeadersPath: "include"),
        .target(name: "KodiAudioCaptureFeature", dependencies: ["CaptureRing"]),
        .testTarget(name: "KodiAudioCaptureFeatureTests", dependencies: ["KodiAudioCaptureFeature", "CaptureRing"])
    ]
)
