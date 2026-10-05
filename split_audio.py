#!/usr/bin/env python3
"""
Split ONE voice-over recording (the whole script) into one file per scene, using the pauses
between scenes.

    python split_audio.py job/video.json path/to/voice.mp3

The recording needs a clear pause (about 2 seconds of silence) between scenes. It finds the speech
parts, expects exactly one per scene that has a "voice" line, cuts them to audio/sN.wav next to
the script JSON, and writes the file names into the JSON.
"""
import json, os, re, subprocess, sys
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()
MIN_SPEECH = 0.30   # ignore blips shorter than this (seconds)


def total_length(path):
    r = subprocess.run([FF, '-i', path], capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if not m:
        sys.exit(f'Cannot read audio file: {path}')
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def speech_segments(path, total, noise_db, min_silence):
    r = subprocess.run([FF, '-i', path, '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
                        '-f', 'null', '-'], capture_output=True, text=True)
    silences, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r'silence_end:\s*(-?[\d.]+)', line)
        if m and cur is not None:
            silences.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        silences.append((cur, total))
    segs, pos = [], 0.0
    for s, e in silences:
        if s - pos >= MIN_SPEECH:
            segs.append((pos, s))
        pos = e
    if total - pos >= MIN_SPEECH:
        segs.append((pos, total))
    return segs


def main():
    if len(sys.argv) < 3:
        sys.exit('usage: split_audio.py job/video.json voice-file')
    jpath, vpath = sys.argv[1], sys.argv[2]
    if not os.path.isfile(vpath):
        sys.exit(f'Voice file not found: {vpath}')
    base = os.path.dirname(os.path.abspath(jpath))
    cfg = json.load(open(jpath, encoding='utf-8'))
    idx = [i for i, s in enumerate(cfg['scenes']) if (s.get('voice') or '').strip()]
    n = len(idx)
    total = total_length(vpath)

    chosen, tried = None, []
    for noise in (-35, -40, -30, -45):
        for gap in (1.4, 1.1, 1.8, 0.9, 2.2, 0.7):
            segs = speech_segments(vpath, total, noise, gap)
            tried.append(len(segs))
            if len(segs) == n:
                chosen = segs
                break
        if chosen:
            break
    if not chosen:
        sys.exit(f'Found {sorted(set(tried))} speech parts, but the script has {n} scenes with voice. '
                 f'Leave a clear pause of about 2 seconds between scenes and send the recording again.')

    os.makedirs(os.path.join(base, 'audio'), exist_ok=True)
    for (a, b), i in zip(chosen, idx):
        rel = f'audio/s{i + 1}.wav'
        r = subprocess.run([FF, '-y', '-ss', f'{max(0.0, a - 0.06):.3f}', '-to', f'{min(total, b + 0.10):.3f}',
                            '-i', vpath, '-c:a', 'pcm_s16le', '-ar', '44100', os.path.join(base, rel)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('ffmpeg failed:\n' + r.stderr[-800:])
        cfg['scenes'][i]['audio'] = rel
        print(f'scene {i + 1}: {b - a:.1f}s -> {rel}')
    json.dump(cfg, open(jpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'split {n} scenes')


if __name__ == '__main__':
    main()
