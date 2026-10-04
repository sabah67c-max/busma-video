#!/usr/bin/env python3
"""
Generate one voice-over file per scene with ElevenLabs, and write the file names into the script JSON.

    python make_audio.py job/video.json

Needs env vars: ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID   (optional: ELEVENLABS_MODEL)
Each scene's spoken text is its "voice" field. pronounce.json (next to this file) can respell
words that the voice mispronounces, e.g. {"تكول": "تگول"}.
"""
import json, os, sys, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get('ELEVENLABS_BASE_URL', 'https://api.elevenlabs.io').rstrip('/')
KEY = os.environ.get('ELEVENLABS_API_KEY', '')
VOICE = os.environ.get('ELEVENLABS_VOICE_ID', '')
MODEL = os.environ.get('ELEVENLABS_MODEL', 'eleven_multilingual_v2')
OUT_FORMAT = 'mp3_44100_128'


def respell(text):
    p = os.path.join(HERE, 'pronounce.json')
    if os.path.exists(p):
        for k, v in json.load(open(p, encoding='utf-8')).items():
            text = text.replace(k, v)
    return text


def tts(text):
    url = f'{API}/v1/text-to-speech/{VOICE}?output_format={OUT_FORMAT}'
    body = json.dumps({'text': text, 'model_id': MODEL}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'xi-api-key': KEY, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f'ElevenLabs error {e.code}: {e.read()[:300].decode("utf-8", "ignore")}')
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** (attempt + 1))
                continue
            sys.exit(f'Network error: {e}')


def main():
    if len(sys.argv) < 2:
        sys.exit('usage: make_audio.py job/video.json')
    if not KEY or not VOICE:
        sys.exit('Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID')
    path = sys.argv[1]
    base = os.path.dirname(os.path.abspath(path))
    cfg = json.load(open(path, encoding='utf-8'))
    os.makedirs(os.path.join(base, 'audio'), exist_ok=True)
    done = 0
    for i, s in enumerate(cfg['scenes'], start=1):
        text = (s.get('voice') or '').strip()
        if not text:
            continue
        data = tts(respell(text))
        rel = f'audio/s{i}.mp3'
        open(os.path.join(base, rel), 'wb').write(data)
        s['audio'] = rel
        done += 1
        print(f'scene {i}: {len(data) // 1024} KB')
    json.dump(cfg, open(path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(f'voice-over done for {done} scenes')


if __name__ == '__main__':
    main()
