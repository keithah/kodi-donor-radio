/* Exercise libavformat's public API without reopening or seeking on EOF. */
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/channel_layout.h>
#include <libavutil/error.h>
#include <libavutil/samplefmt.h>
#include <libavutil/sha.h>
#include <libavutil/time.h>

static int64_t deadline;
static int64_t decoded_frames;
static struct AVSHA *pcm_sha;
static int interrupt_read(void *opaque) { (void)opaque; return av_gettime_relative() >= deadline; }
static void hex(const uint8_t *data, int size, char *out) {
    for (int i = 0; i < size; i++) sprintf(out + 2 * i, "%02x", data[i]);
}
static int receive_frames(AVCodecContext *decoder, AVFrame *frame) {
    int result;
    while ((result = avcodec_receive_frame(decoder, frame)) >= 0) {
        if (frame->format != AV_SAMPLE_FMT_FLTP || frame->sample_rate != 48000 || frame->ch_layout.nb_channels != 2) return AVERROR_INVALIDDATA;
        for (int sample = 0; sample < frame->nb_samples; sample++)
            for (int ch = 0; ch < 2; ch++) av_sha_update(pcm_sha, frame->extended_data[ch] + sample * 4, 4);
        decoded_frames += frame->nb_samples;
        av_frame_unref(frame);
    }
    return result == AVERROR(EAGAIN) || result == AVERROR_EOF ? 0 : result;
}
int main(int argc, char **argv) {
    if (argc != 6) { fprintf(stderr, "usage: probe URL EXPECTED_FRAMES MAX_SECONDS HOLD_LIMIT HTTP_PERSISTENT\n"); return 2; }
    int64_t expected = strtoll(argv[2], NULL, 10);
    deadline = av_gettime_relative() + (int64_t)(strtod(argv[3], NULL) * 1000000);
    AVFormatContext *format = avformat_alloc_context();
    AVCodecContext *decoder = NULL;
    AVPacket *packet = av_packet_alloc();
    AVFrame *frame = av_frame_alloc();
    struct AVSHA *packet_sha = av_sha_alloc();
    pcm_sha = av_sha_alloc();
    AVDictionary *options = NULL;
    int result = 0, audio = -1, packet_count = 0, eof_count = 0;
    if (!format || !packet || !frame || !packet_sha || !pcm_sha) { result = AVERROR(ENOMEM); goto done; }
    format->interrupt_callback.callback = interrupt_read;
    av_sha_init(pcm_sha, 256);
    av_dict_set(&options, "live_start_index", "0", 0);
    av_dict_set(&options, "m3u8_hold_counters", argv[4], 0);
    av_dict_set(&options, "max_reload", "100", 0);
    av_dict_set(&options, "http_persistent", argv[5], 0);
    av_dict_set(&options, "analyzeduration", "100000", 0);
    av_dict_set(&options, "probesize", "32768", 0);
    result = avformat_open_input(&format, argv[1], NULL, &options);
    if (result < 0) goto done;
    result = avformat_find_stream_info(format, NULL);
    if (result < 0) goto done;
    audio = av_find_best_stream(format, AVMEDIA_TYPE_AUDIO, -1, -1, NULL, 0);
    if (audio < 0) { result = audio; goto done; }
    const AVCodec *codec = avcodec_find_decoder(format->streams[audio]->codecpar->codec_id);
    decoder = avcodec_alloc_context3(codec);
    if (!decoder) { result = AVERROR(ENOMEM); goto done; }
    if ((result = avcodec_parameters_to_context(decoder, format->streams[audio]->codecpar)) < 0 ||
        (result = avcodec_open2(decoder, codec, NULL)) < 0) goto done;
    printf("{\"event\":\"open\",\"time_base_num\":%d,\"time_base_den\":%d}\n",format->streams[audio]->time_base.num,format->streams[audio]->time_base.den);
    fflush(stdout);
    for (;;) {
        /* This matches FFmpegStream::DemuxRead's existing AVIO handling. */
        if (format->pb) format->pb->eof_reached = 0;
        result = av_read_frame(format, packet);
        if (result == AVERROR_EOF) {
            eof_count++;
            printf("{\"event\":\"eof\",\"count\":%d,\"packets\":%d,\"frames\":%"PRId64"}\n",eof_count,packet_count,decoded_frames);
            fflush(stdout);
            if (!expected || decoded_frames >= expected) break;
            if (av_gettime_relative() >= deadline) { result = AVERROR_EXIT; break; }
            av_usleep(10000);
            continue;
        }
        if (result < 0) break;
        if (packet->stream_index == audio) {
            uint8_t digest[32]; char digest_hex[65] = {0};
            av_sha_init(packet_sha, 256); av_sha_update(packet_sha, packet->data, packet->size); av_sha_final(packet_sha, digest); hex(digest, 32, digest_hex);
            printf("{\"event\":\"packet\",\"index\":%d,\"pts\":%"PRId64",\"dts\":%"PRId64",\"duration\":%"PRId64",\"size\":%d,\"sha256\":\"%s\"}\n",packet_count++,packet->pts,packet->dts,packet->duration,packet->size,digest_hex);
            result = avcodec_send_packet(decoder, packet);
            if (result >= 0) result = receive_frames(decoder, frame);
            if (result < 0) break;
        }
        av_packet_unref(packet);
    }
    if (result == AVERROR_EOF) {
        result = avcodec_send_packet(decoder, NULL);
        if (result >= 0) result = receive_frames(decoder, frame);
    }
    {
        uint8_t digest[32]; char digest_hex[65] = {0};
        av_sha_final(pcm_sha, digest); hex(digest, 32, digest_hex);
        printf("{\"event\":\"summary\",\"packets\":%d,\"frames\":%"PRId64",\"eof_count\":%d,\"pcm_sha256\":\"%s\",\"result\":%d}\n",packet_count,decoded_frames,eof_count,digest_hex,result);
    }
done:
    if (result < 0) { char error[AV_ERROR_MAX_STRING_SIZE]; av_strerror(result,error,sizeof(error)); fprintf(stderr,"probe error: %s (%d)\n",error,result); }
    av_dict_free(&options); avcodec_free_context(&decoder); avformat_close_input(&format); av_packet_free(&packet); av_frame_free(&frame); av_free(packet_sha); av_free(pcm_sha);
    return result < 0 ? 1 : 0;
}
