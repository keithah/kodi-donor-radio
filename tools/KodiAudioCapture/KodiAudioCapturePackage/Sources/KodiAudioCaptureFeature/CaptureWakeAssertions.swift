import Foundation
import IOKit.pwr_mgt

final class CaptureWakeAssertions {
    private var identifiers: [IOPMAssertionID] = []

    init() throws {
        do {
            for kind in [kIOPMAssertionTypePreventUserIdleSystemSleep, kIOPMAssertionTypePreventUserIdleDisplaySleep] {
                var identifier = IOPMAssertionID(0)
                let result = IOPMAssertionCreateWithName(kind as CFString, IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                                       "Recording a bounded Kodi audio test" as CFString, &identifier)
                guard result == kIOReturnSuccess else { throw CaptureFailure("Unable to keep the Mac awake during this recording (\(result)).") }
                identifiers.append(identifier)
            }
        } catch {
            release()
            throw error
        }
    }

    func release() {
        for identifier in identifiers { IOPMAssertionRelease(identifier) }
        identifiers.removeAll()
    }

    deinit { release() }
}
