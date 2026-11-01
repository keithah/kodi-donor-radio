import Foundation

struct CaptureStartupCancelled: LocalizedError {
    var errorDescription: String? { "Recording was cancelled before audio setup completed." }
}

/// Serializes cancellation against readiness publication; it never interrupts native setup.
final class CaptureStartupGate: @unchecked Sendable {
    private let lock = NSLock()
    private var cancellationRequested = false

    func requestCancellation() {
        lock.withLock { cancellationRequested = true }
    }

    func start(nativeStart: () throws -> Void, publishReady: () throws -> Void) throws {
        try performUnlessCancelled {}
        // Never hold the gate while Core Audio may wait for consent or an IPC reply.
        try nativeStart()
        try performUnlessCancelled(publishReady)
    }

    func performUnlessCancelled<T>(_ operation: () throws -> T) throws -> T {
        lock.lock()
        defer { lock.unlock() }
        guard !cancellationRequested else { throw CaptureStartupCancelled() }
        // Check and publication/activation are one operation relative to cancellation.
        return try operation()
    }
}
