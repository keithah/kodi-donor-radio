import AVFoundation

/// Copies a bounded ring slot into owned PCM on the writer queue, away from the HAL callback.
enum PCMBufferCopy {
    static func copy(format: AVAudioFormat, audioBufferList: UnsafePointer<AudioBufferList>, frameCount: Int) throws -> AVAudioPCMBuffer {
        guard format.commonFormat == .pcmFormatFloat32, format.sampleRate.isFinite, format.sampleRate > 0,
              format.channelCount > 0, frameCount > 0, frameCount <= Int(UInt32.max) else {
            throw CaptureFailure("The capture stream delivered an unsupported PCM format or frame count.")
        }
        let source = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: audioBufferList))
        let expectedBuffers = format.isInterleaved ? 1 : Int(format.channelCount)
        let bytesPerFrame = Int(format.streamDescription.pointee.mBytesPerFrame)
        guard bytesPerFrame > 0, frameCount <= Int.max / bytesPerFrame else { throw CaptureFailure("Invalid PCM byte count.") }
        let expectedBytes = frameCount * bytesPerFrame
        guard source.count == expectedBuffers,
              source.allSatisfy({ $0.mData != nil && Int($0.mDataByteSize) == expectedBytes && $0.mNumberChannels == (format.isInterleaved ? format.channelCount : 1) }),
              let result = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frameCount)) else {
            throw CaptureFailure("PCM buffer sizes do not match the declared sample frame count.")
        }
        result.frameLength = AVAudioFrameCount(frameCount)
        let destination = UnsafeMutableAudioBufferListPointer(result.mutableAudioBufferList)
        for index in source.indices {
            guard let input = source[index].mData, let output = destination[index].mData else { throw CaptureFailure("Missing PCM sample memory.") }
            output.copyMemory(from: input, byteCount: expectedBytes)
        }
        return result
    }
}
