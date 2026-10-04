#!/usr/bin/env python3
"""
Busma video renderer.
Takes a JSON script (scenes + optional per-scene voice-over audio) and renders a
1080x1920 MP4 in the Busma identity (navy / champagne / ivory, Cairo font).

Usage:
    python render.py example/video.json -o out.mp4
    python render.py example/video.json -o out.mp4 --scale 0.5      # fast preview
    python render.py example/video.json --still 5.2 -o frame.png     # single frame
"""
import argparse, json, math, os, random, re, subprocess, sys, tempfile
from multiprocessing import Pool

import cairosvg
import imageio_ffmpeg
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, 'fonts') if os.path.isdir(os.path.join(HERE, 'fonts')) else HERE
AR500 = os.path.join(FONT_DIR, 'cairo-arabic-500.ttf')
AR400 = os.path.join(FONT_DIR, 'cairo-arabic-400.ttf')
LA500 = os.path.join(FONT_DIR, 'cairo-latin-500.ttf')
FF = imageio_ffmpeg.get_ffmpeg_exe()

NAVY, CH, IV = '#0E1B2C', '#E3CFA3', '#F5F1E8'
VW, VH, FPS = 1080, 1920, 30
MAXW = 880
LEAD, TAIL, LAST_TAIL = 0.2, 0.45, 1.3  # seconds around each voice line

# ---- themes / frames / transitions (variety between videos) ----
GOLD_D, GOLD_M = '#6F5520', '#8C6D2A'
THEMES = {
    'navy':      dict(bg=NAVY, fg=IV,   ac=CH,     ln=CH,     pg=IV,   ink=NAVY, st=NAVY, wm=CH),
    'champagne': dict(bg=CH,   fg=NAVY, ac=GOLD_D, ln=NAVY,   pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
    'ivory':     dict(bg=IV,   fg=NAVY, ac=GOLD_M, ln=GOLD_M, pg=CH,   ink=NAVY, st=NAVY, wm=NAVY),
}
FRAMES = ['thin', 'none', 'corners', 'double']
TRANSITIONS = ['fade', 'slide', 'zoom']
TH, STYLE = dict(THEMES['navy']), {}
AR_DIGITS = str.maketrans('0123456789%', '٠١٢٣٤٥٦٧٨٩٪')

# ---------------------------------------------------------------- text -> paths
_font_cache, _text_cache = {}, {}


def _load(path):
    if path not in _font_cache:
        face = hb.Face(hb.Blob.from_file_path(path))
        tt = TTFont(path)
        _font_cache[path] = (hb.Font(face), tt, tt['head'].unitsPerEm)
    return _font_cache[path]


def _text_path(text, path, size, anchor, tracking=0.0):
    hbfont, tt, upem = _load(path)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(hbfont, buf, {'kern': True, 'liga': True})
    gs, order, sc = tt.getGlyphSet(), tt.getGlyphOrder(), size / upem
    width = sum(p.x_advance for p in buf.glyph_positions) * sc + tracking * size * (len(buf.glyph_positions) - 1)
    x0 = -width if anchor == 'end' else (-width / 2 if anchor == 'middle' else 0)
    pen = SVGPathPen(gs, ntos=lambda v: f'{v:.2f}')
    cx = x0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        tpen = TransformPen(pen, (sc, 0, 0, -sc, cx + pos.x_offset * sc, -pos.y_offset * sc))
        gs[order[info.codepoint]].draw(tpen)
        cx += pos.x_advance * sc + tracking * size
    return pen.getCommands(), width


def tp(text, size, anchor='middle', font=None, tracking=0.0):
    k = (text, size, anchor, font or AR500, tracking)
    if k not in _text_cache:
        _text_cache[k] = _text_path(text, font or AR500, size, anchor, tracking)
    return _text_cache[k]


def T(text, size, fill, x, y, op=1.0, dy=0.0, anchor='middle', font=None, tracking=0.0):
    if op <= 0.003:
        return ''
    d, _ = tp(text, size, anchor, font, tracking)
    return f'<path d="{d}" transform="translate({x:.1f} {y + dy:.1f})" fill="{fill}" opacity="{op:.3f}"/>'


def fit_size(text, size, maxw=MAXW, minsize=44, font=None):
    s = size
    while tp(text, s, 'middle', font)[1] > maxw and s > minsize:
        s -= 4
    return s


def wrap_lines(text, size, maxw=MAXW, font=None):
    words, out, cur = text.split(), [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if not cur or tp(trial, size, 'middle', font)[1] <= maxw:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------- brand mark
TP2 = 2 * math.pi


def _r(v):
    return math.floor(v * 100 + 0.5) / 100


def _fmt(pts):
    return 'M ' + ' L '.join(f'{_r(a)} {_r(b)}' for a, b in pts)


def _near(th, c, hw):
    return abs(((th - c + math.pi) % TP2 + TP2) % TP2 - math.pi) < hw


def _ridge_segs(rx, k):
    N, segs, cur = 96, [], []
    g1 = (k * 2.399) % TP2
    g2 = (g1 + 3.1) % TP2
    hw1, hw2 = 0.12 + 0.03 * (k % 3), 0.1
    for i in range(N + 1):
        th = TP2 * i / N
        if _near(th, g1, hw1) or (k >= 3 and _near(th, g2, hw2)):
            if len(cur) > 1:
                segs.append(cur)
            cur = []
            continue
        fc = 1 + 0.03 * math.sin(3 * th + k * 1.3) + 0.018 * math.sin(5 * th + k * 2.1)
        cur.append((rx * fc * math.cos(th), 1.35 * rx * fc * math.sin(th) - 1))
    if len(cur) > 1:
        segs.append(cur)
    return segs


def _spiral(r0, r1):
    pts, total, t = [], 1.3 * TP2, 0.0
    while t <= total:
        s = t / total
        rx = r0 + (r1 - r0) * s
        th = t - math.pi / 2
        pts.append((rx * math.cos(th), 1.35 * rx * math.sin(th) - 1))
        t += 0.15
    return pts


def _plen(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


_sp = _spiral(0.8, 4.0)
FP = [(_fmt(_sp), _plen(_sp))]
for _k in range(1, 4):
    for _sg in _ridge_segs(4.0 + 3.4 * _k, _k):
        FP.append((_fmt(_sg), _plen(_sg)))
FPTOTAL = sum(L for _, L in FP)


def mark_group(c):
    paths = ''.join(f'<path d="{d}"/>' for d, _ in FP)
    return (
        f'<circle cx="50" cy="50" r="46" fill="none" stroke="{c}" stroke-width="1.5"/>'
        f'<circle cx="41.2" cy="41.2" r="24" fill="none" stroke="{c}" stroke-width="3"/>'
        f'<g transform="translate(41.2 41.2)" fill="none" stroke="{c}" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">{paths}</g>'
        f'<g transform="translate(41.2 41.2) rotate(45)"><path d="M22 -6 Q28 -3 33 -3 L49.5 -3 '
        f'A3 3 0 0 1 49.5 3 L33 3 Q28 3 22 6 Z" fill="{c}" stroke="{c}" stroke-width="0.8" '
        f'stroke-linejoin="round"/></g>')


# ---------------------------------------------------------------- helpers
def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def ease(x):
    x = clamp(x)
    return 1 - (1 - x) ** 3


def eio(x):
    x = clamp(x)
    return x * x * (3 - 2 * x)


def fin(t, t0, dur=0.55):
    o = ease((t - t0) / dur)
    return o, (1 - o) * 34


def fp_group(p, scale, tx, ty, color, sw, op=1.0):
    parts, acc = [], 0.0
    for d, L in FP:
        prog = clamp((p * FPTOTAL - acc) / L)
        acc += L
        if prog <= 0:
            continue
        if prog >= 1:
            parts.append(f'<path d="{d}"/>')
        else:
            parts.append(f'<path d="{d}" stroke-dasharray="{L:.2f} {L + 5:.2f}" '
                         f'stroke-dashoffset="{L * (1 - prog):.2f}"/>')
    return (f'<g transform="translate({tx} {ty}) scale({scale})" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" opacity="{op:.3f}">'
            + ''.join(parts) + '</g>')


def card(cx, cy, w, h, fill, op, stroke=None, rx=26):
    s = f' stroke="{stroke}" stroke-width="3"' if stroke else ''
    return (f'<rect x="{cx - w / 2:.1f}" y="{cy - h / 2:.1f}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" opacity="{op:.3f}"{s}/>')


def doc_lines(cx, cy, w, h, color, op, rows=7):
    out = []
    top = cy - h / 2 + 70
    out.append(f'<rect x="{cx + w / 2 - 360:.1f}" y="{top:.1f}" width="300" height="26" rx="13" '
               f'fill="{color}" opacity="{op:.3f}"/>')
    ws = [440, 400, 450, 380, 430, 300, 410]
    for i in range(rows):
        y = top + 80 + i * 56
        x = cx + w / 2 - 60 - ws[i % 7]
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{ws[i % 7]}" height="16" rx="8" '
                   f'fill="{color}" opacity="{op * 0.55:.3f}"/>')
    return ''.join(out)


def hline(x1, x2, y, color, w=6, op=1.0):
    return (f'<line x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}" stroke="{color}" '
            f'stroke-width="{w}" stroke-linecap="round" opacity="{op:.3f}"/>')


# ---------------------------------------------------------------- scene templates
# Each scene gets local time lt (seconds since scene start), its duration D, and its JSON dict s.
def sc_hook(lt, D, s):
    if s.get('variant') == 'words':
        return sc_hook_words(lt, D, s)
    o = fp_group(ease((lt - 0.07 * D) / (0.7 * D)), 13, 540, 800, TH['ac'], 0.95)
    lines = s.get('lines') or wrap_lines(s['text'], 100)
    cols = [TH['fg'], TH['ac'], TH['fg'], TH['ac']]
    for i, ln in enumerate(lines[:3]):
        op, dy = fin(lt, (0.29 + 0.16 * i) * D)
        o += T(ln, fit_size(ln, 100), cols[i], 540, 1360 + 130 * i, op, dy)
    return o


def sc_hook_words(lt, D, s):
    """Kinetic typography: the headline appears word by word, big."""
    size = 150
    lines = wrap_lines(s['text'], size)[:4]
    nl = len(lines)
    words_all = sum(len(l.split()) for l in lines)
    acc_idx = s.get('accent', words_all - 1)
    y0 = 960 - (nl - 1) * 100 + 40
    o, k = '', 0
    for li, ln in enumerate(lines):
        words = ln.split()
        gap = size * 0.26
        widths = [tp(w, size)[1] for w in words]
        x = 540 + (sum(widths) + gap * (len(words) - 1)) / 2
        for w, wd in zip(words, widths):
            t0 = (0.06 + 0.62 * k / max(words_all - 1, 1)) * D
            pr = ease((lt - t0) / 0.38)
            if pr > 0:
                col = TH['ac'] if k == acc_idx else TH['fg']
                d, _ = tp(w, size, 'end')
                sc = 0.86 + 0.14 * pr
                o += (f'<path d="{d}" transform="translate({x:.1f} {y0 + li * 200 + (1 - pr) * 40:.1f}) '
                      f'scale({sc:.3f})" fill="{col}" opacity="{pr:.3f}"/>')
            x -= wd + gap
            k += 1
    g = ease((lt - 0.72 * D) / (0.18 * D))
    o += hline(540 + 150, 540 + 150 - 300 * g, y0 + (nl - 1) * 200 + 90, TH['ac'], 8, g)
    return o


def sc_contrast(lt, D, s):
    o = ''
    sl = eio((lt - 0.42 * D) / (0.2 * D))
    a1 = ease((lt - 0.04 * D) / (0.12 * D)) * (1 - sl)
    cx1 = 540 - 620 * sl
    o += card(cx1, 880, 560, 720, TH['ac'], 0.14 * a1, TH['ac'])
    o += doc_lines(cx1, 880, 560, 720, TH['ac'], 0.55 * a1)
    before = s['before']
    bs = fit_size(before, 110)
    op, dy = fin(lt, 0.07 * D)
    op *= 1 - eio((lt - 0.48 * D) / (0.08 * D))
    o += T(before, bs, TH['ac'], 540, 1480, op * 0.8, dy)
    _, w = tp(before, bs)
    if lt > 0.27 * D and op > 0:
        sp = ease((lt - 0.27 * D) / (0.14 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * sp, 1445, TH['ac'], 7, op)
    b = ease((lt - 0.54 * D) / (0.17 * D))
    yb = 880 + (1 - b) * 90
    o += card(540, yb, 560, 720, TH['pg'], b)
    o += doc_lines(540, yb, 560, 720, TH['ink'], 0.45 * b, 4)
    if b > 0:
        o += fp_group(ease((lt - 0.6 * D) / (0.25 * D)), 6.2, 400, 1030 + (1 - b) * 90, TH['st'], 1.3, b)
    after = s['after']
    op, dy = fin(lt, 0.58 * D)
    o += T(after, fit_size(after, 112), TH['fg'], 540, 1480, op, dy)
    return o


def sc_steps(lt, D, s):
    o = ''
    title = s['title']
    op, dy = fin(lt, 0.04 * D)
    o += T(title, fit_size(title, 124), TH['fg'], 540, 560, op, dy)
    items = s['steps'][:4]
    n = len(items)
    X = 780
    ys = [1100] if n == 1 else [860 + i * (520 / (n - 1)) for i in range(n)]
    starts = [(0.10 + 0.62 * i / max(n - 1, 1)) * D for i in range(n)]
    for i in range(n - 1):
        pr = ease((lt - (starts[i] + 0.07 * D)) / (0.12 * D))
        if pr > 0:
            o += (f'<line x1="{X}" y1="{ys[i] + 38:.1f}" x2="{X}" '
                  f'y2="{ys[i] + 38 + (ys[i + 1] - ys[i] - 76) * pr:.1f}" stroke="{TH["ac"]}" '
                  f'stroke-width="5" stroke-linecap="round"/>')
    for i, label in enumerate(items):
        pr = ease((lt - starts[i]) / 0.5)
        if pr <= 0:
            continue
        o += (f'<circle cx="{X}" cy="{ys[i]:.1f}" r="{34 * (0.7 + 0.3 * pr):.1f}" fill="{TH["bg"]}" '
              f'stroke="{TH["ac"]}" stroke-width="5" opacity="{pr:.3f}"/>'
              f'<circle cx="{X}" cy="{ys[i]:.1f}" r="{14 * pr:.1f}" fill="{TH["ac"]}" opacity="{pr:.3f}"/>')
        op, dy = fin(lt, starts[i] + 0.1)
        o += T(label, fit_size(label, 84, 620), TH['fg'], X - 70, ys[i] + 30, op, dy, 'end')
    return o


def sc_lens(lt, D, s):
    o = ''
    l1, l2 = s['line1'], s['line2']
    op, dy = fin(lt, 0.04 * D)
    o += T(l1, fit_size(l1, 124), TH['fg'], 540, 640, op, dy)
    op, dy = fin(lt, 0.19 * D)
    o += T(l2, fit_size(l2, 124), TH['ac'], 540, 800, op, dy)
    widths, offs = [620, 520, 580, 460, 540], [-130, 100, -70, 150, -110]
    q = eio((lt - 0.29 * D) / (0.6 * D))
    for i in range(5):
        a = eio((q - i / 4 + 0.12) / 0.22)
        x = 900 - widths[i] + offs[i] * (1 - a)
        o += (f'<rect x="{x:.1f}" y="{1060 + i * 80}" width="{widths[i]}" height="34" rx="17" '
              f'fill="{TH["ac"] if a > 0.5 else TH["fg"]}" opacity="{0.32 + 0.6 * a:.3f}"/>')
    la = clamp((lt - 0.27 * D) / 0.3) * clamp((0.93 * D - lt) / 0.3)
    if la > 0:
        lx, ly = 880 - 600 * q, 1077 + 320 * q
        o += (f'<g opacity="{la:.3f}"><circle cx="{lx:.1f}" cy="{ly:.1f}" r="96" fill="{TH["bg"]}" '
              f'fill-opacity="0.35" stroke="{TH["ac"]}" stroke-width="10"/>'
              f'<line x1="{lx + 68:.1f}" y1="{ly + 68:.1f}" x2="{lx + 128:.1f}" y2="{ly + 128:.1f}" '
              f'stroke="{TH["ac"]}" stroke-width="20" stroke-linecap="round"/></g>')
    return o


def sc_stamp(lt, D, s):
    o = ''
    b = ease((lt - 0.04 * D) / (0.15 * D))
    o += card(540, 860, 600, 800, TH['pg'], b)
    o += doc_lines(540, 860, 600, 800, TH['ink'], 0.45 * b, 6)
    st = clamp((lt - 0.30 * D) / (0.14 * D))
    if st > 0:
        o += fp_group(1.0, 7.0 * (1.5 - 0.5 * ease(st)), 690, 1085, TH['st'], 1.3, ease(st))
        pr = ease((lt - 0.43 * D) / (0.18 * D))
        if 0 < pr < 1:
            o += (f'<circle cx="690" cy="1085" r="{130 + 110 * pr:.1f}" fill="none" stroke="{TH["ac"]}" '
                  f'stroke-width="5" opacity="{(1 - pr) * 0.7:.3f}"/>')
    text = s['text']
    op, dy = fin(lt, 0.175 * D)
    o += T(text, fit_size(text, 118), TH['fg'], 540, 1530, op, dy)
    return o


def sc_question(lt, D, s):
    o = ''
    qa = ease((lt - 0.02 * D) / (0.25 * D))
    o += T('؟', 950, TH['ac'], 540, 1190, qa * 0.14, (1 - qa) * 60)
    lines = s.get('lines') or wrap_lines(s['text'], 104)
    lines = lines[:3]
    base = 780 if s.get('answer') else 860
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.10 + 0.12 * i) * D)
        o += T(ln, fit_size(ln, 104), TH['fg'], 540, base + i * 128, op, dy)
    if s.get('answer'):
        ans = s['answer']
        ya = base + len(lines) * 128 + 90
        sz = fit_size(ans, 88)
        op, dy = fin(lt, 0.58 * D)
        o += T(ans, sz, TH['ac'], 540, ya, op, dy)
        _, w = tp(ans, sz)
        g = ease((lt - 0.66 * D) / (0.18 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, ya + 34, TH['ac'], 6, g)
    return o


def sc_bignumber(lt, D, s):
    o = ''
    val = int(s['value'])
    unit = s.get('unit', '')
    cnt = int(round(val * eio((lt - 0.05 * D) / (0.5 * D))))
    final = (str(val) + unit).translate(AR_DIGITS)
    size = fit_size(final, 420, 860, 120)
    if s.get('label'):
        op, dy = fin(lt, 0.02 * D)
        o += T(s['label'], fit_size(s['label'], 76), TH['fg'], 540, 640, op, dy)
    op = ease((lt - 0.02 * D) / (0.1 * D))
    o += T((str(cnt) + unit).translate(AR_DIGITS), size, TH['ac'], 540, 1010, op)
    g = ease((lt - 0.55 * D) / (0.15 * D))
    o += hline(540 + 180, 540 + 180 - 360 * g, 1100, TH['fg'], 6, g * 0.8)
    if s.get('caption'):
        for i, ln in enumerate(wrap_lines(s['caption'], 84)[:3]):
            op, dy = fin(lt, (0.6 + 0.08 * i) * D)
            o += T(ln, fit_size(ln, 84), TH['fg'], 540, 1260 + i * 110, op, dy)
    return o


def sc_checklist(lt, D, s):
    o = ''
    title = s['title']
    op, dy = fin(lt, 0.04 * D)
    o += T(title, fit_size(title, 104), TH['fg'], 540, 500, op, dy)
    items = s['items'][:5]
    n = len(items)
    for i, label in enumerate(items):
        y = 760 + i * 150
        st = (0.12 + 0.6 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.45)
        if pr <= 0:
            continue
        o += (f'<rect x="810" y="{y - 35}" width="70" height="70" rx="16" fill="none" '
              f'stroke="{TH["ac"]}" stroke-width="5" opacity="{pr:.3f}"/>')
        cp = ease((lt - st - 0.15) / 0.4)
        if cp > 0:
            L = 63.1
            o += (f'<polyline points="{845 - 20},{y + 2} {845 - 7},{y + 16} {845 + 21},{y - 18}" fill="none" '
                  f'stroke="{TH["fg"]}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" '
                  f'stroke-dasharray="{L} {L + 5}" stroke-dashoffset="{L * (1 - cp):.2f}"/>')
        op, dy = fin(lt, st + 0.05)
        o += T(label, fit_size(label, 72, 660), TH['fg'], 770, y + 24, op, dy, 'end')
    return o


def sc_versus(lt, D, s):
    o = ''
    cols = [(s['a'], 760, 0.06, 0.14), (s['b'], 320, 0.34, 0.42)]
    g = ease((lt - 0.04 * D) / (0.2 * D))
    o += (f'<line x1="540" y1="560" x2="540" y2="{560 + 940 * g:.1f}" stroke="{TH["ac"]}" '
          f'stroke-width="4" stroke-linecap="round" opacity="0.5"/>')
    dim = 1 - 0.5 * eio((lt - 0.58 * D) / (0.1 * D))
    for ci, (c, cx, tt, pt) in enumerate(cols):
        m = dim if ci == 0 else 1.0
        title = c['title']
        op, dy = fin(lt, tt * D)
        o += T(title, fit_size(title, 76, 400), TH['fg'] if ci == 0 else TH['ac'], cx, 680, op * m, dy)
        for i, p in enumerate(c['points'][:3]):
            op, dy = fin(lt, (pt + 0.07 * i) * D)
            for li, ln in enumerate(wrap_lines(p, 56, 400)[:3]):
                o += T(ln, fit_size(ln, 56, 400), TH['fg'], cx, 860 + i * 230 + li * 68, op * m, dy)
    hl = ease((lt - 0.8 * D) / (0.12 * D))
    if hl > 0:
        o += (f'<rect x="105" y="545" width="430" height="970" rx="30" fill="none" stroke="{TH["ac"]}" '
              f'stroke-width="4" opacity="{hl:.3f}"/>')
    return o


def sc_quote(lt, D, s):
    o = ''
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.02 * D)
        sz = fit_size(by, 54, 600)
        _, w = tp(by, sz)
        o += (f'<rect x="{540 - w / 2 - 36:.1f}" y="{472 + dy:.1f}" width="{w + 72:.1f}" height="92" rx="46" '
              f'fill="none" stroke="{TH["ac"]}" stroke-width="3" opacity="{op:.3f}"/>')
        o += T(by, sz, TH['ac'], 540, 535, op, dy)
    lines = wrap_lines(s['text'], 92, 740)[:5]
    n = len(lines)
    g = ease((lt - 0.05 * D) / (0.3 * D))
    h = n * 135 + 20
    o += (f'<line x1="930" y1="{760:.1f}" x2="930" y2="{760 + h * g:.1f}" stroke="{TH["ac"]}" '
          f'stroke-width="10" stroke-linecap="round"/>')
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.12 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 92, 760), TH['fg'], 880, 850 + i * 135, op, dy, 'end')
    return o


def sc_outro(lt, D, s):
    o = ''
    b = ease((lt - 0.15) / 0.7)
    sc = 5.6 * (0.92 + 0.08 * b)
    o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {740 - 50 * sc:.1f}) '
          f'scale({sc:.3f})">{mark_group(TH["wm"])}</g>')
    name, slogan, cta = s.get('name', 'بصمة'), s.get('slogan', 'بصمتك على بحثك'), s.get('cta', 'راسلنا هسه')
    latin = s.get('latin', 'busma')
    op, dy = fin(lt, 0.7)
    o += T(name, 210, TH['fg'], 540, 1225, op, dy)
    if latin:
        op, dy = fin(lt, 0.95)
        o += T(latin, 58, TH['fg'], 540, 1312, op * 0.75, dy, font=LA500, tracking=0.32)
    op, dy = fin(lt, 1.25)
    o += T(slogan, 64, TH['ac'], 540, 1415, op, dy, font=AR400)
    op, dy = fin(lt, 1.7)
    if op > 0:
        o += (f'<rect x="300" y="{1525 + dy:.1f}" width="480" height="116" rx="58" fill="none" '
              f'stroke="{TH["ac"]}" stroke-width="4" opacity="{op:.3f}"/>')
        o += T(cta, fit_size(cta, 62, 400), TH['fg'], 540, 1602, op, dy)
    return o


SCENE_TYPES = {'hook': sc_hook, 'contrast': sc_contrast, 'steps': sc_steps, 'lens': sc_lens,
               'stamp': sc_stamp, 'question': sc_question, 'bignumber': sc_bignumber,
               'checklist': sc_checklist, 'versus': sc_versus, 'quote': sc_quote, 'outro': sc_outro}

# ---------------------------------------------------------------- plan + frames
CFG = {}


def probe(path):
    r = subprocess.run([FF, '-i', path], capture_output=True, text=True)
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    if not m:
        sys.exit(f'Cannot read audio duration: {path}')
    h, mn, sec = m.groups()
    return int(h) * 3600 + int(mn) * 60 + float(sec)


def plan(cfg, base):
    scenes, t = [], 0.0
    n = len(cfg['scenes'])
    for i, s in enumerate(cfg['scenes']):
        if s['type'] not in SCENE_TYPES:
            sys.exit(f"Unknown scene type '{s['type']}'. Valid: {', '.join(SCENE_TYPES)}")
        apath = os.path.join(base, s['audio']) if s.get('audio') else None
        alen = probe(apath) if apath else float(s.get('duration', 3.0))
        D = LEAD + alen + TAIL + (LAST_TAIL if i == n - 1 else 0.0)
        scenes.append(dict(s, start=t, dur=D, audio_path=apath, audio_start=t + LEAD))
        t += D
    return scenes, t


def pick_style(cfg):
    st = dict(theme='navy', frame='thin', transition='fade', watermark='top')
    if 'seed' in cfg:
        r = random.Random(cfg['seed'])
        st.update(theme=r.choices(list(THEMES), weights=[5, 3, 2])[0],
                  frame=r.choice(FRAMES), transition=r.choice(TRANSITIONS))
    st.update(cfg.get('style', {}))
    return st


def frame_deco():
    c, f = TH['ln'], STYLE['frame']
    if f == 'none':
        return ''
    if f == 'thin':
        return (f'<rect x="40" y="40" width="1000" height="1840" rx="30" fill="none" stroke="{c}" '
                f'stroke-width="2" opacity="0.8"/>')
    if f == 'double':
        return (f'<rect x="40" y="40" width="1000" height="1840" rx="30" fill="none" stroke="{c}" '
                f'stroke-width="3" opacity="0.85"/>'
                f'<rect x="60" y="60" width="960" height="1800" rx="22" fill="none" stroke="{c}" '
                f'stroke-width="1.5" opacity="0.5"/>')
    d = ('M52 170 V52 H170 M1028 170 V52 H910 M52 1750 V1868 H170 M1028 1750 V1868 H910')
    return (f'<path d="{d}" fill="none" stroke="{c}" stroke-width="5" stroke-linecap="round" '
            f'stroke-linejoin="round" opacity="0.9"/>')


def frame_svg(t):
    scenes = CFG['scenes']
    body = f'<rect width="{VW}" height="{VH}" fill="{TH["bg"]}"/>' + frame_deco()
    outro = next((s for s in scenes if s['type'] == 'outro'), None)
    wm = 1.0 if not outro else 1 - ease((t - (outro['start'] - 0.2)) / 0.3)
    if STYLE['watermark'] != 'none' and wm > 0:
        body += f'<g opacity="{wm * 0.9:.3f}" transform="translate(490 90)">{mark_group(TH["wm"])}</g>'
    tr = STYLE['transition']
    for i, s in enumerate(scenes):
        lt = t - s['start']
        oi = clamp((lt + 0.05) / 0.28)
        oo = clamp((s['dur'] + 0.28 - lt) / 0.28) if i < len(scenes) - 1 else 1.0
        o = oi * oo
        if o <= 0:
            continue
        tf = ''
        if tr == 'slide':
            tf = f' transform="translate(0 {(1 - oi) * 70 - (1 - oo) * 70:.1f})"'
        elif tr == 'zoom':
            k = 0.94 + 0.06 * oi + (1 - oo) * 0.06
            tf = f' transform="translate(540 960) scale({k:.4f}) translate(-540 -960)"'
        body += f'<g opacity="{o:.3f}"{tf}>{SCENE_TYPES[s["type"]](lt, s["dur"], s)}</g>'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VW} {VH}" '
            f'width="{VW}" height="{VH}">{body}</svg>')


def render_frame(i):
    return cairosvg.svg2png(bytestring=frame_svg(i / FPS).encode(), output_width=CFG['out_w'])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('script')
    ap.add_argument('-o', '--out', default='out.mp4')
    ap.add_argument('--scale', type=float, default=1.0, help='0.5 = fast half-size preview')
    ap.add_argument('--workers', type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument('--still', type=float, help='render a single frame (seconds) as PNG')
    a = ap.parse_args()

    base = os.path.dirname(os.path.abspath(a.script))
    cfg = json.load(open(a.script, encoding='utf-8'))
    STYLE.update(pick_style(cfg))
    TH.update(THEMES[STYLE['theme']])
    print('style:', STYLE)
    scenes, total = plan(cfg, base)
    CFG.update(scenes=scenes, total=total, out_w=int(VW * a.scale))
    print(f'scenes: {len(scenes)}  total: {total:.1f}s')

    if a.still is not None:
        open(a.out, 'wb').write(render_frame(int(a.still * FPS)))
        print('still ->', a.out)
        return

    n = int(math.ceil(total * FPS))
    tmp = tempfile.mktemp(suffix='.mp4')
    pr = subprocess.Popen([FF, '-y', '-f', 'image2pipe', '-framerate', str(FPS), '-i', '-',
                           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'medium',
                           '-movflags', '+faststart', tmp], stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    with Pool(a.workers) as pool:
        for i, png in enumerate(pool.imap(render_frame, range(n), chunksize=4)):
            pr.stdin.write(png)
            if i % 90 == 0:
                print(f'frame {i}/{n}', flush=True)
    pr.stdin.close()
    pr.wait()

    audios = [s for s in scenes if s['audio_path']]
    if not audios:
        os.replace(tmp, a.out)
    else:
        cmd, filt, labels = [FF, '-y', '-i', tmp], [], []
        for k, s in enumerate(audios, start=1):
            cmd += ['-i', s['audio_path']]
            ms = int(s['audio_start'] * 1000)
            filt.append(f'[{k}:a]aresample=44100,adelay={ms}|{ms}[a{k}]')
            labels.append(f'[a{k}]')
        filt.append(''.join(labels) + f'amix=inputs={len(labels)}:duration=longest,volume={len(labels)}[aout]')
        cmd += ['-filter_complex', ';'.join(filt), '-map', '0:v', '-map', '[aout]',
                '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-t', f'{total:.2f}', a.out]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit('audio mux failed:\n' + r.stderr[-1500:])
        os.remove(tmp)
    print('done ->', a.out)


if __name__ == '__main__':
    main()
