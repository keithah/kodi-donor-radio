import Foundation

struct CaptureFailure: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct CaptureOptions: Sendable {
    var pid: Int32?
    var duration: Double = 30
    var outputRoot = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Movies/KodiAudioCapture", isDirectory: true)
    var autoRecord = false
    var exitAfterRecording = false

    init(arguments: [String]) throws {
        var index = 0
        func value(after option: String) throws -> String {
            index += 1
            guard index < arguments.count else { throw CaptureFailure("Missing value for \(option).") }
            return arguments[index]
        }
        while index < arguments.count {
            let argument = arguments[index]
            switch argument {
            case "--pid":
                guard let number = Int32(try value(after: argument)), number > 0 else { throw CaptureFailure("Choose a positive Kodi process ID.") }
                pid = number
            case "--duration":
                guard let number = Double(try value(after: argument)), number.isFinite, (1...1200).contains(number) else {
                    throw CaptureFailure("Recording duration must be between 1 and 1200 seconds.")
                }
                duration = number
            case "--output-root":
                let path = try value(after: argument)
                guard path.hasPrefix("/") else { throw CaptureFailure("The output folder must be an absolute path.") }
                outputRoot = URL(fileURLWithPath: path, isDirectory: true).standardizedFileURL
            case "--auto-record": autoRecord = true
            case "--exit-after-recording": exitAfterRecording = true
            default:
                if argument.hasPrefix("-psn_") { break }
                if argument.hasPrefix("-"), !argument.hasPrefix("--") {
                    // Cocoa NSArgumentDomain default: "-Key value".
                    if index + 1 < arguments.count { index += 1 }
                    break
                }
                throw CaptureFailure("Unknown launch option: \(argument)")
            }
            index += 1
        }
        guard !autoRecord || pid != nil else { throw CaptureFailure("Automatic recording requires an explicit --pid.") }
        guard !exitAfterRecording || autoRecord else { throw CaptureFailure("--exit-after-recording requires --auto-record.") }
    }
}

struct KodiProcess: Identifiable, Hashable, Codable, Sendable {
    var id: Int32 { pid }
    let pid: Int32
    let name: String
    let bundlePath: String
    let executablePath: String
    let launchTime: TimeInterval
}
