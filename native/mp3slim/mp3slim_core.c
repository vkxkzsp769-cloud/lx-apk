/* mp3slim_core.c —— MP3 有损压缩核心（纯 C，无 ffmpeg 依赖）
 *
 * 流程：解码(dr_libs) → 下混/重采样 → LAME 重新编码为 MP3
 *
 * 为什么要自己写而不是调 ffmpeg 命令：
 *   Android 应用目录是 noexec 的，ffmpeg 可执行文件跑不起来；
 *   p4a 里也没有能用的 ffmpeg recipe。所以把「解码 + 重采样 + 编码」
 *   做成一个 .so，Python 用 ctypes 直接调，零 shell 依赖。
 *
 * 解码器（都是单头文件、无外部依赖、许可宽松）：
 *   dr_mp3  (public domain / MIT-0)  .mp3
 *   dr_flac (public domain / MIT-0)  .flac
 *   dr_wav  (public domain / MIT-0)  .wav
 * 编码器：
 *   LAME 3.100 (LGPL) —— 与 ffmpeg 的 libmp3lame 同一实现，音质一致
 *
 * 对外只有三个符号（见文件末尾 extern "C"）：
 *   mp3slim_run()          压缩一个文件
 *   mp3slim_probe()        读时长/采样率/声道/码率（给界面显示）
 *   mp3slim_last_error()   上一次失败的原因
 */

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define DR_MP3_IMPLEMENTATION
#define DR_FLAC_IMPLEMENTATION
#define DR_WAV_IMPLEMENTATION
#define DR_MP3_ONLY
#include "codec/dr/dr_mp3.h"
#include "codec/dr/dr_flac.h"
#include "codec/dr/dr_wav.h"

#include "codec/lame/lame.h"
#include "codec/lame/lame_global_flags.h"

#define SLIM_OK 0
#define SLIM_ERR_ARG -1
#define SLIM_ERR_OPEN -2
#define SLIM_ERR_FORMAT -3
#define SLIM_ERR_DECODE -4
#define SLIM_ERR_ENCODE -5
#define SLIM_ERR_IO -6
#define SLIM_ERR_MEM -7

#define RS_TAPS 24
#define RS_PHASES 128
#define BLOCK_FRAMES 4096

static char g_err[512];

static void set_err(const char *fmt, const char *a, long b)
{
    /* 不依赖 snprintf 的可变参数版本，避免不同 libc 差异 */
    if (a && b)
        snprintf(g_err, sizeof(g_err), fmt, a, b);
    else if (a)
        snprintf(g_err, sizeof(g_err), fmt, a);
    else
        snprintf(g_err, sizeof(g_err), "%s", fmt);
    g_err[sizeof(g_err) - 1] = 0;
}

const char *mp3slim_last_error(void) { return g_err; }

/* ───────────────────────── 重采样器 ─────────────────────────
 * 24 抽头加窗 sinc，128 相位。够用且开销小（一首歌毫秒级）。
 * 只在需要降采样时才启用；同采样率直接透传。
 */
typedef struct {
    int in_rate, out_rate;
    int channels;
    int enabled;
    float kern[RS_PHASES][RS_TAPS];
    double step;        /* 每个输出样本消耗的输入样本数 */
    long long j;        /* 下一个输出样本序号 */
    long long n_in;     /* 已喂入输入总帧数 */
    float hist[2][RS_TAPS];  /* 每声道的历史尾巴 */
    int hist_len;
    float *scratch[2];
    int scratch_cap;
} rs_t;

static double sinc_pi(double x)
{
    if (fabs(x) < 1e-9)
        return 1.0;
    x *= M_PI;
    return sin(x) / x;
}

static int rs_init(rs_t *r, int in_rate, int out_rate, int channels)
{
    memset(r, 0, sizeof(*r));
    r->in_rate = in_rate;
    r->out_rate = out_rate;
    r->channels = channels > 2 ? 2 : channels;
    if (channels < 1)
        r->channels = 1;
    r->step = (double)in_rate / (double)out_rate;
    r->enabled = (in_rate != out_rate);
    r->scratch_cap = BLOCK_FRAMES + RS_TAPS * 2;
    for (int c = 0; c < 2; c++) {
        r->scratch[c] = (float *)calloc((size_t)r->scratch_cap, sizeof(float));
        if (!r->scratch[c])
            return SLIM_ERR_MEM;
    }
    if (!r->enabled)
        return SLIM_OK;

    /* 抗混叠：截止频率取两者较小值，留 6% 过渡带 */
    double cutoff = 1.0;
    if (out_rate < in_rate)
        cutoff = (double)out_rate / (double)in_rate;
    cutoff *= 0.94;
    const double center = (RS_TAPS - 1) / 2.0;
    for (int p = 0; p < RS_PHASES; p++) {
        double frac = (double)p / RS_PHASES;
        double sum = 0.0;
        for (int t = 0; t < RS_TAPS; t++) {
            double x = (double)t - center + (1.0 - frac);
            double w = cutoff * sinc_pi(cutoff * x);
            /* Blackman 窗，压低旁瓣（比矩形窗干净得多） */
            double u = (double)t / (RS_TAPS - 1);
            w *= 0.42 - 0.5 * cos(2.0 * M_PI * u) + 0.08 * cos(4.0 * M_PI * u);
            r->kern[p][t] = (float)w;
            sum += w;
        }
        if (fabs(sum) < 1e-9)
            sum = 1.0;
        for (int t = 0; t < RS_TAPS; t++)
            r->kern[p][t] = (float)(r->kern[p][t] / sum);
    }
    return SLIM_OK;
}

static void rs_free(rs_t *r)
{
    for (int c = 0; c < 2; c++) {
        free(r->scratch[c]);
        r->scratch[c] = NULL;
    }
}

typedef int (*emit_fn)(void *ud, const int16_t *interleaved, int frames);

/* 把一个解码块喂进重采样器，产出的 PCM 交给 emit()
 * in: 交错 s16，frames 帧，channels 声道（1 或 2） */
static int rs_push(rs_t *r, const int16_t *in, int frames, emit_fn emit, void *ud,
                   int16_t *outbuf, int outcap)
{
    if (frames <= 0)
        return SLIM_OK;
    if (!r->enabled) {
        /* 采样率一致：只做下混（需要的话），直接交给编码器 */
        if (r->channels == 2 && 2 == r->channels) {
            return emit(ud, in, frames);
        }
        for (int i = 0; i < frames; i++) {
            outbuf[i] = in[i];
        }
        return emit(ud, outbuf, frames);
    }

    int n = frames;
    int cap = r->scratch_cap;
    if (n > cap - RS_TAPS * 2)
        n = cap - RS_TAPS * 2;

    for (int c = 0; c < r->channels; c++) {
        float *dst = r->scratch[c];
        int base = r->hist_len;
        memcpy(dst, r->hist[c], sizeof(float) * (size_t)base);
        for (int i = 0; i < n; i++) {
            float v;
            if (r->channels == 1)
                v = (float)in[i];
            else
                v = (float)in[i * 2 + c];
            dst[base + i] = v;
        }
    }

    int total = r->hist_len + n;
    int produced = 0;
    const double half = (RS_TAPS - 1) / 2.0;

    while (1) {
        double center = (double)r->j * r->step;
        double rel = center - (double)r->n_in;      /* 相对本块起点 */
        if (rel + half + 1.0 >= (double)n)
            break;
        double abs_pos = rel + (double)r->hist_len; /* scratch 坐标 */
        long i0 = (long)floor(abs_pos);
        double frac = abs_pos - (double)i0;
        int phase = (int)(frac * RS_PHASES + 0.5);
        if (phase >= RS_PHASES) {
            phase -= RS_PHASES;
            i0 += 1;
        }
        long start = i0 - (RS_TAPS / 2 - 1);
        if (start < 0)
            start = 0;
        if (start + RS_TAPS > total)
            break;
        for (int c = 0; c < r->channels; c++) {
            const float *src = r->scratch[c] + start;
            const float *kw = r->kern[phase];
            float acc = 0.0f;
            for (int t = 0; t < RS_TAPS; t++)
                acc += src[t] * kw[t];
            if (acc > 32767.0f)
                acc = 32767.0f;
            if (acc < -32768.0f)
                acc = -32768.0f;
            if (r->channels == 1)
                outbuf[produced] = (int16_t)lrintf(acc);
            else
                outbuf[produced * 2 + c] = (int16_t)lrintf(acc);
        }
        produced++;
        r->j++;
        if (produced * r->channels >= outcap) {
            int rc = emit(ud, outbuf, produced);
            if (rc != SLIM_OK)
                return rc;
            produced = 0;
        }
    }
    if (produced) {
        int rc = emit(ud, outbuf, produced);
        if (rc != SLIM_OK)
            return rc;
    }

    /* 保存尾巴当下一块的历史 */
    r->n_in += n;
    int keep = total < RS_TAPS ? total : RS_TAPS;
    for (int c = 0; c < r->channels; c++)
        memcpy(r->hist[c], r->scratch[c] + (total - keep), sizeof(float) * (size_t)keep);
    r->hist_len = keep;
    return SLIM_OK;
}

/* ───────────────────────── 编码上下文 ───────────────────────── */
typedef struct {
    lame_t gf;
    FILE *out;
    int channels;
    long long frames_done;
    long long frames_total;   /* 估算值，用于进度 */
    long long audio_start;    /* LAME/Xing 标签回填位置 */
    int (*cb)(double frac, void *ud);
    void *ud;
    int16_t *pcm;
    int pcm_cap;
    unsigned char *mp3buf;
    int mp3cap;
} enc_t;

static int enc_write(enc_t *e, const unsigned char *data, int len)
{
    if (len <= 0)
        return SLIM_OK;
    if (fwrite(data, 1, (size_t)len, e->out) != (size_t)len)
        return SLIM_ERR_IO;
    return SLIM_OK;
}

static int enc_emit(void *ud, const int16_t *pcm, int frames)
{
    enc_t *e = (enc_t *)ud;
    if (frames <= 0)
        return SLIM_OK;
    int n;
    if (e->channels == 2)
        n = lame_encode_buffer_interleaved(e->gf, (short int *)pcm, frames,
                                           e->mp3buf, e->mp3cap);
    else
        n = lame_encode_buffer(e->gf, (short int *)pcm, (short int *)pcm,
                               frames, e->mp3buf, e->mp3cap);
    if (n < 0) {
        set_err("编码失败(LAME %ld)", NULL, (long)n);
        return SLIM_ERR_ENCODE;
    }
    if (n > 0 && enc_write(e, e->mp3buf, n) != SLIM_OK) {
        set_err("写入输出文件失败", NULL, 0);
        return SLIM_ERR_IO;
    }
    e->frames_done += frames;
    if (e->cb && e->frames_total > 0) {
        double frac = (double)e->frames_done / (double)e->frames_total;
        if (frac > 0.999)
            frac = 0.999;
        e->cb(frac, e->ud);
    }
    return SLIM_OK;
}

/* ───────────────────────── 探测文件信息 ───────────────────────── */
typedef struct {
    long long duration_ms;
    int sample_rate;
    int channels;
    int kbps;
    const char *codec;
} probe_t;

static int detect_kind(const char *path, char *head, int n)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        return -1;
    int got = (int)fread(head, 1, (size_t)n, f);
    fclose(f);
    if (got < 4)
        return -1;
    if (!memcmp(head, "ID3", 3))
        return 1;
    unsigned char b0 = (unsigned char)head[0], b1 = (unsigned char)head[1];
    if (b0 == 0xFF && (b1 & 0xE0) == 0xE0)
        return 1;
    if (!memcmp(head, "fLaC", 4))
        return 2;
    if (!memcmp(head, "RIFF", 4) && got >= 12 && !memcmp(head + 8, "WAVE", 4))
        return 3;
    if (!memcmp(head, "OggS", 4))
        return 4;
    if (got >= 12 && !memcmp(head + 4, "ftyp", 4))
        return 5;
    return 0;
}

static int probe_mp3(const char *path, probe_t *p)
{
    drmp3 mp3;
    if (!drmp3_init_file(&mp3, path, NULL))
        return SLIM_ERR_FORMAT;
    p->sample_rate = (int)mp3.sampleRate;
    p->channels = (int)mp3.channels;
    drmp3_uint64 total = drmp3_get_pcm_frame_count(&mp3);
    p->duration_ms = (long long)(total * 1000 / (mp3.sampleRate ? mp3.sampleRate : 44100));
    /* 码率按文件大小估（含标签，偏大一点，够界面用） */
    FILE *f = fopen(path, "rb");
    long long size = 0;
    if (f) {
        fseek(f, 0, SEEK_END);
        size = (long long)ftell(f);
        fclose(f);
    }
    if (p->duration_ms > 0)
        p->kbps = (int)(size * 8 / p->duration_ms);
    p->codec = "mp3";
    drmp3_uninit(&mp3);
    return SLIM_OK;
}

static int probe_flac(const char *path, probe_t *p)
{
    drflac *f = drflac_open_file(path, NULL);
    if (!f)
        return SLIM_ERR_FORMAT;
    p->sample_rate = (int)f->sampleRate;
    p->channels = (int)f->channels;
    p->duration_ms = (long long)(f->totalPCMFrameCount * 1000
                                 / (f->sampleRate ? f->sampleRate : 44100));
    p->kbps = 0;
    p->codec = "flac";
    drflac_close(f);
    return SLIM_OK;
}

static int probe_wav(const char *path, probe_t *p)
{
    drwav wav;
    if (!drwav_init_file(&wav, path, NULL))
        return SLIM_ERR_FORMAT;
    p->sample_rate = (int)wav.sampleRate;
    p->channels = (int)wav.channels;
    p->duration_ms = (long long)(wav.totalPCMFrameCount * 1000
                                 / (wav.sampleRate ? wav.sampleRate : 44100));
    p->kbps = (int)((wav.sampleRate * wav.channels * wav.bitsPerSample) / 1000);
    p->codec = "wav";
    drwav_uninit(&wav);
    return SLIM_OK;
}

static int probe_file(const char *path, probe_t *p)
{
    char head[16];
    memset(p, 0, sizeof(*p));
    int kind = detect_kind(path, head, (int)sizeof(head));
    switch (kind) {
    case 1: return probe_mp3(path, p);
    case 2: return probe_flac(path, p);
    case 3: return probe_wav(path, p);
    case 4:
        set_err("OGG 暂不支持，请先转成 MP3", NULL, 0);
        return SLIM_ERR_FORMAT;
    case 5:
        set_err("M4A/AAC 暂不支持，请先转成 MP3", NULL, 0);
        return SLIM_ERR_FORMAT;
    case -1:
        set_err("打不开文件", NULL, 0);
        return SLIM_ERR_OPEN;
    default:
        set_err("不是可识别的音频格式", NULL, 0);
        return SLIM_ERR_FORMAT;
    }
}

/* 根据目标码率挑输出采样率（低码率降采样才不刺耳，也才真的是「小而干净」） */
static int pick_out_rate(int in_rate, int bitrate, int user_rate)
{
    if (user_rate > 0)
        return user_rate;
    int want = in_rate;
    if (bitrate > 0 && bitrate <= 48)
        want = 22050;
    else if (bitrate > 0 && bitrate <= 64)
        want = 32000;
    /* 绝不升采样 */
    if (want > in_rate)
        want = in_rate;
    /* LAME 只支持这几档 */
    static const int ok[] = {8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000};
    int best = 44100;
    int best_d = 1 << 30;
    for (unsigned i = 0; i < sizeof(ok) / sizeof(ok[0]); i++) {
        int d = ok[i] > want ? ok[i] - want : want - ok[i];
        if (d < best_d) {
            best_d = d;
            best = ok[i];
        }
    }
    return best;
}

static int run_mp3(enc_t *e, const char *src, rs_t *rs, int16_t *pcm,
                   int16_t *outbuf, int outcap, int in_ch)
{
    drmp3 mp3;
    if (!drmp3_init_file(&mp3, src, NULL)) {
        set_err("MP3 解码器打不开文件", NULL, 0);
        return SLIM_ERR_DECODE;
    }
    int rc = SLIM_OK;
    for (;;) {
        drmp3_uint64 got = drmp3_read_pcm_frames_s16(&mp3, BLOCK_FRAMES, pcm);
        if (got == 0)
            break;
        rc = rs_push(rs, pcm, (int)got, enc_emit, e, outbuf, outcap);
        if (rc != SLIM_OK)
            break;
    }
    drmp3_uninit(&mp3);
    (void)in_ch;
    return rc;
}

static int run_flac(enc_t *e, const char *src, rs_t *rs, int16_t *pcm,
                    int16_t *outbuf, int outcap)
{
    drflac *f = drflac_open_file(src, NULL);
    if (!f) {
        set_err("FLAC 解码器打不开文件", NULL, 0);
        return SLIM_ERR_DECODE;
    }
    int rc = SLIM_OK;
    for (;;) {
        drflac_uint64 got = drflac_read_pcm_frames_s16(f, BLOCK_FRAMES, pcm);
        if (got == 0)
            break;
        rc = rs_push(rs, pcm, (int)got, enc_emit, e, outbuf, outcap);
        if (rc != SLIM_OK)
            break;
    }
    drflac_close(f);
    return rc;
}

static int run_wav(enc_t *e, const char *src, rs_t *rs, int16_t *pcm,
                   int16_t *outbuf, int outcap)
{
    drwav wav;
    if (!drwav_init_file(&wav, src, NULL)) {
        set_err("WAV 解码器打不开文件", NULL, 0);
        return SLIM_ERR_DECODE;
    }
    int rc = SLIM_OK;
    for (;;) {
        drwav_uint64 got = drwav_read_pcm_frames_s16(&wav, BLOCK_FRAMES, pcm);
        if (got == 0)
            break;
        rc = rs_push(rs, pcm, (int)got, enc_emit, e, outbuf, outcap);
        if (rc != SLIM_OK)
            break;
    }
    drwav_uninit(&wav);
    return rc;
}

/* ───────────────────────── 主入口 ─────────────────────────
 * bitrate: 目标 CBR 码率 kbps（8..320）
 * mono:    1 = 强制单声道（人声/播客再省一半）
 * out_rate: 0 = 自动；否则用指定采样率
 * cb:      进度回调（0..1）
 * 返回 0 成功；负数见 SLIM_ERR_*
 */
int mp3slim_run(const char *src, const char *dst, int bitrate, int mono,
                int out_rate, long long *out_size, long long *out_ms,
                int (*cb)(double frac, void *ud), void *ud)
{
    if (!src || !dst || bitrate <= 0) {
        set_err("参数不合法", NULL, 0);
        return SLIM_ERR_ARG;
    }
    g_err[0] = 0;

    probe_t info;
    int rc = probe_file(src, &info);
    if (rc != SLIM_OK)
        return rc;
    if (info.sample_rate <= 0 || info.channels <= 0) {
        set_err("读不到音频参数", NULL, 0);
        return SLIM_ERR_FORMAT;
    }

    int out_ch = mono ? 1 : (info.channels > 1 ? 2 : 1);
    int o_rate = pick_out_rate(info.sample_rate, bitrate, out_rate);
    if (out_ch == 1 && bitrate > 96) {
        /* 单声道不需要高码率，但用户明确选了就照做 */
    }

    enc_t e;
    memset(&e, 0, sizeof(e));
    e.channels = out_ch;
    e.cb = cb;
    e.ud = ud;
    e.frames_total = info.duration_ms > 0
                         ? info.duration_ms * o_rate / 1000 : 0;

    e.pcm_cap = BLOCK_FRAMES * 2;
    e.pcm = (int16_t *)calloc((size_t)e.pcm_cap, sizeof(int16_t));
    e.mp3cap = (int)(1.25 * 16384 + 7200);
    e.mp3buf = (unsigned char *)calloc((size_t)e.mp3cap, 1);
    int16_t *outbuf = (int16_t *)calloc((size_t)(BLOCK_FRAMES * 2), sizeof(int16_t));
    rs_t rs;
    int rs_ok = rs_init(&rs, info.sample_rate, o_rate, out_ch == 1 ? 1 : 2);
    if (!e.pcm || !e.mp3buf || !outbuf || rs_ok != SLIM_OK) {
        free(e.pcm);
        free(e.mp3buf);
        free(outbuf);
        rs_free(&rs);
        set_err("内存不足", NULL, 0);
        return SLIM_ERR_MEM;
    }

    e.gf = lame_init();
    if (!e.gf) {
        set_err("LAME 初始化失败", NULL, 0);
        rc = SLIM_ERR_ENCODE;
        goto done;
    }
    lame_set_in_samplerate(e.gf, o_rate);
    lame_set_num_channels(e.gf, out_ch);
    lame_set_mode(e.gf, out_ch == 1 ? MONO : JOINT_STEREO);
    lame_set_brate(e.gf, bitrate);
    lame_set_quality(e.gf, 2);        /* 2 = 高质量，速度可接受 */
    lame_set_write_id3tag_automatic(e.gf, 0);
    if (lame_init_params(e.gf) < 0) {
        set_err("LAME 参数不被接受（码率 %ld 与采样率不匹配）", NULL, (long)bitrate);
        rc = SLIM_ERR_ENCODE;
        goto done;
    }

    e.out = fopen(dst, "wb");
    if (!e.out) {
        set_err("无法创建输出文件", NULL, 0);
        rc = SLIM_ERR_OPEN;
        goto done;
    }
    e.audio_start = 0;

    {
        char head[16];
        int kind = detect_kind(src, head, (int)sizeof(head));
        if (kind == 1)
            rc = run_mp3(&e, src, &rs, e.pcm, outbuf, BLOCK_FRAMES * 2, info.channels);
        else if (kind == 2)
            rc = run_flac(&e, src, &rs, e.pcm, outbuf, BLOCK_FRAMES * 2);
        else if (kind == 3)
            rc = run_wav(&e, src, &rs, e.pcm, outbuf, BLOCK_FRAMES * 2);
        else {
            set_err("不支持的输入格式", NULL, 0);
            rc = SLIM_ERR_FORMAT;
        }
    }

    if (rc == SLIM_OK) {
        int n = lame_encode_flush(e.gf, e.mp3buf, e.mp3cap);
        if (n < 0) {
            set_err("收尾编码失败", NULL, 0);
            rc = SLIM_ERR_ENCODE;
        } else if (n > 0 && enc_write(&e, e.mp3buf, n) != SLIM_OK) {
            rc = SLIM_ERR_IO;
        }
    }

    if (rc == SLIM_OK && e.out) {
        /* 把 LAME/Xing 信息帧写回开头（播放器据此显示时长、支持精确 seek） */
        FILE *rf = e.out;
        long long end = -1;
        int tag = lame_get_lametag_frame(e.gf, e.mp3buf, (size_t)e.mp3cap);
        if (tag > 0) {
            fflush(rf);
            end = (long long)ftell(rf);
            if (fseek(rf, (long)e.audio_start, SEEK_SET) == 0)
                fwrite(e.mp3buf, 1, (size_t)tag, rf);
            if (end >= 0)
                fseek(rf, (long)end, SEEK_SET);
        }
    }

done:
    if (e.out) {
        fclose(e.out);
        e.out = NULL;
    }
    if (rc == SLIM_OK) {
        FILE *f = fopen(dst, "rb");
        long long size = 0;
        if (f) {
            fseek(f, 0, SEEK_END);
            size = (long long)ftell(f);
            fclose(f);
        }
        if (size <= 0) {
            set_err("输出文件为空", NULL, 0);
            rc = SLIM_ERR_IO;
        }
        if (out_size)
            *out_size = size;
        if (out_ms) {
            long long frames = e.frames_done;
            *out_ms = o_rate > 0 ? frames * 1000 / o_rate : 0;
        }
        if (cb)
            cb(1.0, ud);
    } else if (dst) {
        remove(dst);      /* 失败不留半截文件 */
    }
    if (e.gf)
        lame_close(e.gf);
    free(e.pcm);
    free(e.mp3buf);
    free(outbuf);
    rs_free(&rs);
    return rc;
}

/* 只探测（界面显示用）。返回 0 成功。 */
int mp3slim_probe(const char *path, long long *duration_ms, int *sample_rate,
                  int *channels, int *kbps, const char **codec)
{
    if (!path) {
        set_err("参数不合法", NULL, 0);
        return SLIM_ERR_ARG;
    }
    g_err[0] = 0;
    probe_t p;
    int rc = probe_file(path, &p);
    if (rc != SLIM_OK)
        return rc;
    if (duration_ms)
        *duration_ms = p.duration_ms;
    if (sample_rate)
        *sample_rate = p.sample_rate;
    if (channels)
        *channels = p.channels;
    if (kbps)
        *kbps = p.kbps;
    if (codec)
        *codec = p.codec;
    return SLIM_OK;
}

/* 估算给定码率下的输出大小（字节），用于「压完大概多大」的提示 */
long long mp3slim_estimate(long long duration_ms, int bitrate, int mono)
{
    if (duration_ms <= 0 || bitrate <= 0)
        return 0;
    double bits = (double)bitrate * 1000.0 * ((double)duration_ms / 1000.0);
    if (mono)
        bits *= 1.0;              /* 码率已含声道，单声道在码率上体现 */
    return (long long)(bits / 8.0);
}
