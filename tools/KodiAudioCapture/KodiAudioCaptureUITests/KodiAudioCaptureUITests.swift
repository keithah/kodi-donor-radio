import XCTest

final class KodiAudioCaptureUITests: XCTestCase {
    @MainActor
    func testRecorderShowsControlsWithoutStartingCapture() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.staticTexts["recordingStatus"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["startRecording"].exists)
        XCTAssertTrue(app.buttons["Open recording folder"].exists)
    }
}
