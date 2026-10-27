import CoreAudio
import CoreMedia

struct HALTimestamp: Encodable, Sendable {
    let sampleTime: Double?
    let hostTime: UInt64
    let rateScalar: Double?
    let wordClockTime: UInt64
    let flags: UInt32

    init(_ timestamp: AudioTimeStamp) {
        sampleTime = timestamp.mFlags.contains(.sampleTimeValid) && timestamp.mSampleTime.isFinite ? timestamp.mSampleTime : nil
        hostTime = timestamp.mHostTime
        rateScalar = timestamp.mFlags.contains(.rateScalarValid) && timestamp.mRateScalar.isFinite ? timestamp.mRateScalar : nil
        wordClockTime = timestamp.mWordClockTime
        flags = timestamp.mFlags.rawValue
    }
}

struct CaptureAudioTiming {
    let input: HALTimestamp
    let callback: HALTimestamp
    let hostPTS: CMTime
    let callbackHostNS: UInt64

    init(inputTime: AudioTimeStamp, callbackTime: AudioTimeStamp, arrivalHostTime: UInt64) throws {
        guard inputTime.mFlags.contains(.hostTimeValid),
              !inputTime.mFlags.contains(.sampleTimeValid) || inputTime.mSampleTime.isFinite else {
            throw CaptureFailure("Core Audio did not provide a valid input host timestamp.")
        }
        input = HALTimestamp(inputTime)
        callback = HALTimestamp(callbackTime)
        hostPTS = CMClockMakeHostTimeFromSystemUnits(inputTime.mHostTime)
        callbackHostNS = UInt64(max(0, try captureNanoseconds(CMClockMakeHostTimeFromSystemUnits(arrivalHostTime))))
    }
}
