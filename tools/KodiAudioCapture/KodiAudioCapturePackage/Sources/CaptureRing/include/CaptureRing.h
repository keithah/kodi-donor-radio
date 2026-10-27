#ifndef KODI_CAPTURE_RING_H
#define KODI_CAPTURE_RING_H

#include <CoreAudio/CoreAudio.h>
#include <stdbool.h>
#include <stdint.h>

typedef struct CaptureRing CaptureRing;
typedef struct CaptureDeviceCallback CaptureDeviceCallback;
typedef struct {
    AudioTimeStamp inputTime;
    AudioTimeStamp callbackTime;
    uint64_t arrivalHostTime;
    uint32_t frameCount;
    AudioBufferList *buffers;
} CaptureSlot;

enum {
    CaptureRingFailureNone = 0,
    CaptureRingFailureOverflow = 1,
    CaptureRingFailureLayout = 2,
    CaptureRingFailureTimestamp = 3
};

CaptureRing *CaptureRingCreate(uint32_t capacity, uint32_t maximumFrames, uint32_t bufferCount, uint32_t channelsPerBuffer, uint32_t bytesPerFrame);
void CaptureRingDestroy(CaptureRing *ring);
bool CaptureRingPush(CaptureRing *ring, const AudioBufferList *audio, const AudioTimeStamp *inputTime, const AudioTimeStamp *callbackTime, uint64_t arrivalHostTime);
const CaptureSlot *CaptureRingPeek(CaptureRing *ring);
void CaptureRingConsume(CaptureRing *ring);
int CaptureRingGetFailure(CaptureRing *ring);
OSStatus CaptureDeviceCallbackCreate(AudioObjectID device, CaptureRing *ring, CaptureDeviceCallback **result);
OSStatus CaptureDeviceCallbackStart(CaptureDeviceCallback *callback);
OSStatus CaptureDeviceCallbackStop(CaptureDeviceCallback *callback);
OSStatus CaptureDeviceCallbackDestroy(CaptureDeviceCallback *callback);
void CaptureDeviceCallbackForget(CaptureDeviceCallback *callback);
OSStatus CaptureRingIOProc(AudioObjectID device, const AudioTimeStamp *now, const AudioBufferList *input, const AudioTimeStamp *inputTime, AudioBufferList *output, const AudioTimeStamp *outputTime, void *context);

#endif
