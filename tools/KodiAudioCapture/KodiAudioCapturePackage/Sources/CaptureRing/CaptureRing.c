#include "CaptureRing.h"
#include <mach/mach_time.h>
#include <math.h>
#include <stdatomic.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

struct CaptureRing {
    uint32_t capacity;
    uint32_t maximumFrames;
    uint32_t bufferCount;
    uint32_t channelsPerBuffer;
    uint32_t bytesPerFrame;
    CaptureSlot *slots;
    _Atomic uint64_t head;
    _Atomic uint64_t tail;
    _Atomic int failure;
};

static bool fail(CaptureRing *ring, int code) {
    int expected = CaptureRingFailureNone;
    atomic_compare_exchange_strong_explicit(&ring->failure, &expected, code, memory_order_release, memory_order_relaxed);
    return false;
}

CaptureRing *CaptureRingCreate(uint32_t capacity, uint32_t maximumFrames, uint32_t bufferCount, uint32_t channelsPerBuffer, uint32_t bytesPerFrame) {
    if (!capacity || capacity > 128 || !maximumFrames || maximumFrames > 65536 ||
        !bufferCount || bufferCount > 2 || !channelsPerBuffer || channelsPerBuffer > 2 ||
        !bytesPerFrame || bytesPerFrame > 8) return NULL;
    CaptureRing *ring = calloc(1, sizeof(*ring));
    if (!ring) return NULL;
    ring->capacity = capacity;
    ring->maximumFrames = maximumFrames;
    ring->bufferCount = bufferCount;
    ring->channelsPerBuffer = channelsPerBuffer;
    ring->bytesPerFrame = bytesPerFrame;
    atomic_init(&ring->head, 0);
    atomic_init(&ring->tail, 0);
    atomic_init(&ring->failure, CaptureRingFailureNone);
    ring->slots = calloc(capacity, sizeof(CaptureSlot));
    if (!ring->slots) { free(ring); return NULL; }
    for (uint32_t index = 0; index < capacity; index++) {
        AudioBufferList *buffers = calloc(1, offsetof(AudioBufferList, mBuffers) + bufferCount * sizeof(AudioBuffer));
        if (!buffers) { CaptureRingDestroy(ring); return NULL; }
        ring->slots[index].buffers = buffers;
        buffers->mNumberBuffers = bufferCount;
        for (uint32_t buffer = 0; buffer < bufferCount; buffer++) {
            buffers->mBuffers[buffer].mNumberChannels = channelsPerBuffer;
            buffers->mBuffers[buffer].mData = malloc((size_t)maximumFrames * bytesPerFrame);
            if (!buffers->mBuffers[buffer].mData) { CaptureRingDestroy(ring); return NULL; }
        }
    }
    return ring;
}

void CaptureRingDestroy(CaptureRing *ring) {
    if (!ring) return;
    for (uint32_t index = 0; index < ring->capacity; index++) {
        AudioBufferList *buffers = ring->slots[index].buffers;
        if (!buffers) continue;
        for (uint32_t buffer = 0; buffer < buffers->mNumberBuffers; buffer++) free(buffers->mBuffers[buffer].mData);
        free(buffers);
    }
    free(ring->slots);
    free(ring);
}

bool CaptureRingPush(CaptureRing *ring, const AudioBufferList *audio, const AudioTimeStamp *inputTime, const AudioTimeStamp *callbackTime, uint64_t arrivalHostTime) {
    if (!ring || atomic_load_explicit(&ring->failure, memory_order_acquire)) return false;
    // An idle tap can deliver no samples. That becomes an explicit empty-recording failure at finalization.
    if (!audio || !audio->mNumberBuffers) return false;
    if (audio->mNumberBuffers != ring->bufferCount) return fail(ring, CaptureRingFailureLayout);
    uint32_t byteCount = audio->mBuffers[0].mDataByteSize;
    if (!byteCount) return false;
    if (byteCount % ring->bytesPerFrame) return fail(ring, CaptureRingFailureLayout);
    uint32_t frames = byteCount / ring->bytesPerFrame;
    if (frames > ring->maximumFrames) return fail(ring, CaptureRingFailureLayout);
    for (uint32_t index = 0; index < ring->bufferCount; index++) {
        const AudioBuffer *buffer = &audio->mBuffers[index];
        if (!buffer->mData || buffer->mDataByteSize != byteCount || buffer->mNumberChannels != ring->channelsPerBuffer)
            return fail(ring, CaptureRingFailureLayout);
    }
    if (!inputTime || !callbackTime || !(inputTime->mFlags & kAudioTimeStampHostTimeValid) ||
        ((inputTime->mFlags & kAudioTimeStampSampleTimeValid) && !isfinite(inputTime->mSampleTime)))
        return fail(ring, CaptureRingFailureTimestamp);
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_relaxed);
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_acquire);
    if (head - tail >= ring->capacity) return fail(ring, CaptureRingFailureOverflow);
    CaptureSlot *slot = &ring->slots[head % ring->capacity];
    slot->inputTime = *inputTime;
    slot->callbackTime = *callbackTime;
    slot->arrivalHostTime = arrivalHostTime;
    slot->frameCount = frames;
    for (uint32_t index = 0; index < ring->bufferCount; index++) {
        memcpy(slot->buffers->mBuffers[index].mData, audio->mBuffers[index].mData, byteCount);
        slot->buffers->mBuffers[index].mDataByteSize = byteCount;
    }
    atomic_store_explicit(&ring->head, head + 1, memory_order_release);
    return true;
}

const CaptureSlot *CaptureRingPeek(CaptureRing *ring) {
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_relaxed);
    uint64_t head = atomic_load_explicit(&ring->head, memory_order_acquire);
    return tail == head ? NULL : &ring->slots[tail % ring->capacity];
}

void CaptureRingConsume(CaptureRing *ring) {
    uint64_t tail = atomic_load_explicit(&ring->tail, memory_order_relaxed);
    atomic_store_explicit(&ring->tail, tail + 1, memory_order_release);
}

int CaptureRingGetFailure(CaptureRing *ring) { return atomic_load_explicit(&ring->failure, memory_order_acquire); }

OSStatus CaptureRingIOProc(AudioObjectID device, const AudioTimeStamp *now, const AudioBufferList *input, const AudioTimeStamp *inputTime, AudioBufferList *output, const AudioTimeStamp *outputTime, void *context) {
    (void)device; (void)output; (void)outputTime;
    CaptureRingPush(context, input, inputTime, now, mach_absolute_time());
    return noErr;
}

struct CaptureDeviceCallback {
    AudioObjectID device;
    AudioDeviceIOProcID callback;
};

OSStatus CaptureDeviceCallbackCreate(AudioObjectID device, CaptureRing *ring, CaptureDeviceCallback **result) {
    *result = NULL;
    CaptureDeviceCallback *registration = calloc(1, sizeof(*registration));
    if (!registration) return kAudioHardwareUnspecifiedError;
    registration->device = device;
    OSStatus status = AudioDeviceCreateIOProcID(device, CaptureRingIOProc, ring, &registration->callback);
    if (status != noErr) { free(registration); return status; }
    *result = registration;
    return noErr;
}

OSStatus CaptureDeviceCallbackStart(CaptureDeviceCallback *callback) {
    return AudioDeviceStart(callback->device, callback->callback);
}

OSStatus CaptureDeviceCallbackStop(CaptureDeviceCallback *callback) {
    return AudioDeviceStop(callback->device, callback->callback);
}

OSStatus CaptureDeviceCallbackDestroy(CaptureDeviceCallback *callback) {
    OSStatus status = AudioDeviceDestroyIOProcID(callback->device, callback->callback);
    if (status == noErr) free(callback);
    return status;
}

void CaptureDeviceCallbackForget(CaptureDeviceCallback *callback) { free(callback); }
