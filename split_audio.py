#!/usr/bin/env python3
"""
Split ONE voice-over recording (the whole script) into one file per scene.

    python split_audio.py job/video.json path/to/voice.mp3

How it works: it finds every pause in the recording, then chooses the pauses that best match the
scene boundaries. The choice uses the length of each scene's voice text (a longer text gets more of
the recording), so it does NOT depend on special long pauses. Long pauses (about 2 seconds) are
preferred when they exist. Output: audio/sN.wav next to the script JSON, and the names are written
into the JSON.
"""
import itertools, json, os, re, subprocess, sys
import imageio_ffmpeg

FF = imageio_ffmpeg.get_ffmpeg_exe()


def total_length(path):
    r = subprocess.run([FF, '-i', path], capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if not m:
        sys.exit(f'Cannot read audio file: {path}')
    h, mn, s = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(s)


def find_silences(path, total, noise_db, min_silence):
    r = subprocess.run([FF, '-i', path, '-af', f'silencedetect=noise={noise_db}dB:d={min_silence}',
                        '-f', 'null', '-'], capture_output=True, text=True)
    out, cur = [], None
    for line in r.stderr.splitlines():
        m = re.search(r'silence_start:\s*(-?[\d.]+)', line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r'silence_end:\s*(-?[\d.]+)', line)
        if m and cur is not None:
            out.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        out.append((cur, total))
    return out


def weight(text):
    return max(1, len(re.sub(r'[\W_]+', '', text)))


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

    # speech region (ignore leading and trailing silence)
    sil = []
    for noise, gap in ((-35, 0.35), (-35, 0.25), (-30, 0.25), (-40, 0.2)):
        sil = find_silences(vpath, total, noise, gap)
        inner = [(a, b) for a, b in sil if a > 0.15 and b < total - 0.15]
        if len(inner) >= n - 1:
            break
    lead = sil[0][1] if sil and sil[0][0] <= 0.15 else 0.0
    tail = sil[-1][0] if sil and sil[-1][1] >= total - 0.15 else total
    inner = [(a, b) for a, b in sil if a > 0.15 and b < total - 0.15]
    if len(inner) < n - 1:
        sys.exit(f'Found only {len(inner)} pauses in the recording, but the script has {n} scenes. '
                 f'Leave a short pause between scenes and send the recording again.')

    texts = [cfg['scenes'][i]['voice'] for i in idx]
    wts = [weight(t) for t in texts]
    speech = max(0.1, tail - lead)
    # expected cut positions from text length
    exp, acc = [], 0
    for w in wts[:-1]:
        acc += w
        exp.append(lead + speech * acc / sum(wts))

    best, best_cost = None, None
    if n == 1:
        best = ()
    else:
        for combo in itertools.combinations(range(len(inner)), n - 1):
            cost = 0.0
            for k, ci in enumerate(combo):
                a, b = inner[ci]
                cost += abs((a + b) / 2 - exp[k]) - 1.5 * (b - a)
            if best_cost is None or cost < best_cost:
                best, best_cost = combo, cost

    bounds = [(lead, None)]
    cuts = [inner[ci] for ci in best]
    starts = [lead] + [b for a, b in cuts]
    ends = [a for a, b in cuts] + [tail]
    os.makedirs(os.path.join(base, 'audio'), exist_ok=True)
    for (a, b), i in zip(zip(starts, ends), idx):
        if b - a < 0.5:
            sys.exit(f'Scene {i + 1} came out only {b - a:.1f}s long. Check the recording matches the script.')
        rel = f'audio/s{i + 1}.wav'
        r = subprocess.run([FF, '-y', '-ss', f'{max(0.0, a - 0.05):.3f}', '-to', f'{min(total, b + 0.12):.3f}',
                            '-i', vpath, '-c:a', 'pcm_s16le', '-ar', '44100', os.path.join(base, rel)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('ffmpeg failed:\n' + r.stderr[-800:])
        cfg['scenes'][i]['audio'] = rel
        print(f'scene {i + 1}: {a:.1f}s -> {b:.1f}s ({b - a:.1f}s) -> {rel}')
    json.dump(cfg, open(jpath, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'split {n} scenes')


if __name__ == '__main__':
    main()
