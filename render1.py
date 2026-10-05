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

# ---- design system: themes / backgrounds / frames / transitions / reveals / scene variants ----
GOLD_D, GOLD_M = '#6F5520', '#8C6D2A'
THEMES = {
    'navy':      dict(bg=NAVY,      fg=IV,   ac=CH,     ln=CH,     pg=IV,   ink=NAVY, st=NAVY, wm=CH),
    'champagne': dict(bg=CH,        fg=NAVY, ac=GOLD_D, ln=NAVY,   pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
    'ivory':     dict(bg=IV,        fg=NAVY, ac=GOLD_M, ln=GOLD_M, pg=CH,   ink=NAVY, st=NAVY, wm=NAVY),
    'night':     dict(bg='#08111C', fg=IV,   ac=CH,     ln=CH,     pg=IV,   ink=NAVY, st=NAVY, wm=CH),
    'sand':      dict(bg='#F1E6CC', fg=NAVY, ac=GOLD_D, ln=GOLD_D, pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
    'ink':       dict(bg=NAVY,      fg=CH,   ac=IV,     ln=IV,     pg=CH,   ink=NAVY, st=NAVY, wm=IV),
}
FRAMES = ['thin', 'double', 'corners', 'none', 'bars', 'rails']
TRANSITIONS = ['fade', 'slide', 'zoom', 'slidex', 'rise', 'wipe']
BACKGROUNDS = ['solid', 'dots', 'grid', 'diag', 'rings', 'waves', 'orbs', 'print']
REVEALS = ['rise', 'fade', 'pop', 'slidex']
VARIANTS = {
    'hook': ['print', 'words', 'box', 'typewriter'],
    'contrast': ['pages', 'slider', 'cards'],
    'steps': ['timeline', 'cards', 'numerals', 'pills'],
    'lens': ['bars', 'spotlight'],
    'stamp': ['page', 'seal'],
    'question': ['bigmark', 'bubble', 'ring'],
    'bignumber': ['count', 'dots'],
    'checklist': ['boxes', 'pills', 'numerals', 'cards'],
    'versus': ['columns', 'stack', 'flip'],
    'quote': ['bar', 'marks', 'card'],
    'outro': ['center', 'minimal', 'row'],
}
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
    d, w = tp(text, size, anchor, font, tracking)
    mode = STYLE.get('reveal', 'rise')
    p = 1.0 if dy == 0 else 1 - clamp(dy / 34.0)
    if mode == 'rise':
        tf = f'translate({x:.1f} {y + dy:.1f})'
    elif mode == 'fade':
        tf = f'translate({x:.1f} {y:.1f})'
    elif mode == 'slidex':
        tf = f'translate({x - (1 - p) * 70:.1f} {y:.1f})'
    elif mode == 'drop':
        tf = f'translate({x:.1f} {y - dy:.1f})'
    elif mode in ('zoomout', 'stretch', 'pop'):
        cx = x - w / 2 if anchor == 'end' else (x + w / 2 if anchor == 'start' else x)
        if mode == 'pop':
            kx = ky = 0.86 + 0.14 * p
        elif mode == 'zoomout':
            kx = ky = 1.16 - 0.16 * p
        else:
            kx, ky = 1.28 - 0.28 * p, 1.0
        tf = f'translate({cx:.1f} {y:.1f}) scale({kx:.3f} {ky:.3f}) translate({-cx:.1f} {-y:.1f}) translate({x:.1f} {y:.1f})'
    else:
        tf = f'translate({x:.1f} {y + dy:.1f})'
    return f'<path d="{d}" transform="{tf}" fill="{fill}" opacity="{op:.3f}"/>'


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
# Every scene type has several visual variants (see VARIANTS); s['variant'] picks one.
def numeral(i):
    return str(i).translate(AR_DIGITS)


def circ(cx, cy, r, fill='none', stroke=None, sw=4, op=1.0):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ''
    return f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{fill}"{st} opacity="{op:.3f}"/>'


def rrect(x, y, w, h, rx, fill='none', stroke=None, sw=4, op=1.0):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ''
    return (f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}"{st} '
            f'opacity="{op:.3f}"/>')


def pill_text(text, cx, cy, size, filled, op=1.0, dy=0.0):
    _, w = tp(text, size)
    h = size * 1.55
    o = ''
    if filled:
        o += rrect(cx - w / 2 - 36, cy - h * 0.68 + dy, w + 72, h, h / 2, fill=TH['ac'], op=op)
        o += T(text, size, TH['bg'], cx, cy, op, dy)
    else:
        o += rrect(cx - w / 2 - 36, cy - h * 0.68 + dy, w + 72, h, h / 2, stroke=TH['ac'], sw=3, op=op)
        o += T(text, size, TH['ac'], cx, cy, op, dy)
    return o


# ---- shared list renderer (steps + checklist) ----------------------------------------------
def render_list(lt, D, items, style, ytop):
    if style in EXTRA_LISTS:
        return EXTRA_LISTS[style](lt, D, items, ytop)
    o = ''
    n = len(items)
    starts = [(0.10 + 0.62 * i / max(n - 1, 1)) * D for i in range(n)]
    if style == 'timeline':
        X = 780
        ys = [ytop + (i * 520 / (n - 1) if n > 1 else 240) for i in range(n)]
        for i in range(n - 1):
            pr = ease((lt - (starts[i] + 0.07 * D)) / (0.12 * D))
            if pr > 0:
                o += (f'<line x1="{X}" y1="{ys[i] + 38:.1f}" x2="{X}" y2="{ys[i] + 38 + (ys[i + 1] - ys[i] - 76) * pr:.1f}" '
                      f'stroke="{TH["ac"]}" stroke-width="5" stroke-linecap="round"/>')
        for i, label in enumerate(items):
            pr = ease((lt - starts[i]) / 0.5)
            if pr <= 0:
                continue
            o += circ(X, ys[i], 34 * (0.7 + 0.3 * pr), fill=TH['bg'], stroke=TH['ac'], sw=5, op=pr)
            o += circ(X, ys[i], 14 * pr, fill=TH['ac'], op=pr)
            op, dy = fin(lt, starts[i] + 0.1)
            o += T(label, fit_size(label, 84, 620), TH['fg'], X - 70, ys[i] + 30, op, dy, 'end')
    elif style == 'boxes':
        for i, label in enumerate(items):
            y = ytop + i * 150
            pr = ease((lt - starts[i]) / 0.45)
            if pr <= 0:
                continue
            o += rrect(810, y - 35, 70, 70, 16, stroke=TH['ac'], sw=5, op=pr)
            cp = ease((lt - starts[i] - 0.15) / 0.4)
            if cp > 0:
                L = 63.1
                o += (f'<polyline points="{845 - 20},{y + 2} {845 - 7},{y + 16} {845 + 21},{y - 18}" fill="none" '
                      f'stroke="{TH["fg"]}" stroke-width="8" stroke-linecap="round" stroke-linejoin="round" '
                      f'stroke-dasharray="{L} {L + 5}" stroke-dashoffset="{L * (1 - cp):.2f}"/>')
            op, dy = fin(lt, starts[i] + 0.05)
            o += T(label, fit_size(label, 72, 660), TH['fg'], 770, y + 24, op, dy, 'end')
    elif style == 'cards':
        h = 150
        for i, label in enumerate(items):
            y = ytop + i * 178
            pr = ease((lt - starts[i]) / 0.5)
            if pr <= 0:
                continue
            dx = (1 - pr) * (220 if i % 2 == 0 else -220)
            g = f'<g transform="translate({dx:.1f} 0)" opacity="{pr:.3f}">'
            g += rrect(90, y - h / 2, 900, h, 30, stroke=TH['ac'], sw=4)
            g += circ(900, y, 46, fill=TH['ac'])
            g += T(numeral(i + 1), 58, TH['bg'], 900, y + 20)
            g += T(label, fit_size(label, 70, 600), TH['fg'], 830, y + 24, 1, 0, 'end')
            o += g + '</g>'
    elif style == 'numerals':
        for i, label in enumerate(items):
            y = ytop + i * 165
            pr = ease((lt - starts[i]) / 0.5)
            if pr <= 0:
                continue
            o += T(numeral(i + 1), 190, TH['ac'], 130, y + 44, pr * 0.30, (1 - pr) * 34, 'start')
            op, dy = fin(lt, starts[i] + 0.08)
            o += T(label, fit_size(label, 72, 680), TH['fg'], 850, y + 20, op, dy, 'end')
            o += hline(960, 960 - 840 * pr, y + 70, TH['fg'], 2, 0.22)
    else:  # pills
        for i, label in enumerate(items):
            y = ytop + 30 + i * 140
            pr = ease((lt - starts[i]) / 0.45)
            if pr <= 0:
                continue
            sz = fit_size(label, 64, 760)
            _, w = tp(label, sz)
            k = 0.82 + 0.18 * pr
            o += (f'<g transform="translate(540 {y}) scale({k:.3f}) translate(-540 {-y})" opacity="{pr:.3f}">'
                  + rrect(540 - w / 2 - 55, y - 62, w + 110, 100, 50, fill=TH['ac'])
                  + T(label, sz, TH['bg'], 540, y + 8) + '</g>')
    return o


# ---- hook ------------------------------------------------------------------------------------
def sc_hook(lt, D, s):
    v = s.get('variant') or 'print'
    if v == 'words':
        return sc_hook_words(lt, D, s)
    if v == 'box':
        return sc_hook_box(lt, D, s)
    if v == 'typewriter':
        return sc_hook_type(lt, D, s)
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
    text = s.get('text') or ' '.join(s.get('lines', []))
    lines = wrap_lines(text, size)[:4]
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


def sc_hook_box(lt, D, s):
    o = fp_group(ease((lt - 0.05 * D) / (0.55 * D)), 6.5, 540, 560, TH['ac'], 1.1)
    lines = (s.get('lines') or wrap_lines(s['text'], 92, 760))[:3]
    n = len(lines)
    h = n * 132 + 80
    y0 = 820
    bx = ease((lt - 0.18 * D) / (0.18 * D))
    o += rrect(120, y0, 840, h * bx, 36, stroke=TH['ac'], sw=5, op=bx)
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.30 + 0.14 * i) * D)
        o += T(ln, fit_size(ln, 92, 760), TH['fg'] if i % 2 == 0 else TH['ac'], 540, y0 + 112 + i * 132, op, dy)
    return o


def sc_hook_type(lt, D, s):
    o = fp_group(ease((lt - 0.05 * D) / (0.5 * D)), 5.0, 540, 560, TH['ac'], 1.0)
    text = s.get('text') or ' '.join(s.get('lines', []))
    lines = wrap_lines(text, 104, 860)[:3]
    n = len(lines)
    y0 = 960 - (n - 1) * 70 + 60
    p = clamp((lt - 0.12 * D) / (0.62 * D))
    for i, ln in enumerate(lines):
        pi = clamp(p * n - i)
        if pi <= 0:
            continue
        sz = fit_size(ln, 104, 860)
        d, w = tp(ln, sz)
        y = y0 + i * 140
        xr = 540 + w / 2
        x0 = xr - w * pi - 6
        o += (f'<clipPath id="tw{i}"><rect x="{x0:.1f}" y="{y - sz:.1f}" width="{w * pi + 12:.1f}" height="{sz * 1.7:.1f}"/></clipPath>'
              f'<g clip-path="url(#tw{i})"><path d="{d}" transform="translate(540 {y})" fill="{TH["fg"] if i % 2 == 0 else TH["ac"]}"/></g>')
        blink = 1.0 if pi < 1 else (0.5 + 0.5 * math.sin(lt * 9))
        if i == n - 1 or pi < 1:
            o += (f'<line x1="{xr - w * pi:.1f}" y1="{y - sz * 0.8:.1f}" x2="{xr - w * pi:.1f}" y2="{y + sz * 0.25:.1f}" '
                  f'stroke="{TH["ac"]}" stroke-width="7" stroke-linecap="round" opacity="{blink:.3f}"/>')
    return o


# ---- contrast --------------------------------------------------------------------------------
def sc_contrast(lt, D, s):
    v = s.get('variant') or 'pages'
    if v == 'slider':
        return sc_contrast_slider(lt, D, s)
    if v == 'cards':
        return sc_contrast_cards(lt, D, s)
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


def sc_contrast_slider(lt, D, s):
    o = ''
    before, after = s['before'], s['after']
    p = eio((lt - 0.34 * D) / (0.30 * D))
    op, dy = fin(lt, 0.05 * D)
    bs = fit_size(before, 120)
    o += T(before, bs, TH['fg'], 540, 760, op * (1 - 0.6 * p), dy)
    _, w = tp(before, bs)
    if p > 0:
        o += hline(540 + w / 2, 540 + w / 2 - w * p, 722, TH['ac'], 8, op)
    t1, t2 = 230, 850
    tr = ease((lt - 0.05 * D) / (0.12 * D))
    o += hline(t1, t2, 990, TH['fg'], 6, 0.25 * tr)
    kx = t2 - (t2 - t1) * p
    if p > 0:
        o += hline(t2, kx, 990, TH['ac'], 9, 1.0)
    o += circ(kx, 990, 30, fill=TH['ac'], op=tr)
    o += circ(kx, 990, 12, fill=TH['bg'], op=tr)
    op, dy = fin(lt, 0.52 * D)
    o += T(after, fit_size(after, 132), TH['ac'], 540, 1290, op * clamp(p * 1.5), dy)
    g = ease((lt - 0.64 * D) / (0.16 * D))
    _, wa = tp(after, fit_size(after, 132))
    o += hline(540 + wa / 2, 540 + wa / 2 - wa * g, 1335, TH['fg'], 6, g * 0.8)
    return o


def sc_contrast_cards(lt, D, s):
    o = ''
    before, after = s['before'], s['after']
    a = ease((lt - 0.05 * D) / (0.14 * D))
    fade = 1 - 0.5 * eio((lt - 0.40 * D) / (0.1 * D))
    o += rrect(110, 560, 860, 300, 40, stroke=TH['fg'], sw=4, op=a * 0.5 * fade)
    o += T(before, fit_size(before, 96, 760), TH['fg'], 540, 735, a * 0.75 * fade, (1 - a) * 34)
    g = ease((lt - 0.30 * D) / (0.12 * D))
    _, w = tp(before, fit_size(before, 96, 760))
    o += hline(540 + w / 2, 540 + w / 2 - w * g, 705, TH['ac'], 7, g)
    ar = ease((lt - 0.42 * D) / (0.12 * D))
    o += (f'<polyline points="500,{930 - 10} 540,{930 + 28} 580,{930 - 10}" fill="none" stroke="{TH["ac"]}" '
          f'stroke-width="9" stroke-linecap="round" stroke-linejoin="round" opacity="{ar:.3f}"/>')
    b = ease((lt - 0.50 * D) / (0.16 * D))
    k = 0.9 + 0.1 * b
    o += (f'<g transform="translate(540 1230) scale({k:.3f}) translate(-540 -1230)" opacity="{b:.3f}">'
          + rrect(110, 1080, 860, 300, 40, fill=TH['ac'])
          + T(after, fit_size(after, 108, 760), TH['bg'], 540, 1255) + '</g>')
    return o


# ---- steps / checklist (shared list styles) -----------------------------------------------------
def sc_steps(lt, D, s):
    title = s['title']
    op, dy = fin(lt, 0.04 * D)
    o = T(title, fit_size(title, 124), TH['fg'], 540, 560, op, dy)
    return o + render_list(lt, D, s['steps'][:4], s.get('variant') or 'timeline', 860)


def sc_checklist(lt, D, s):
    title = s['title']
    op, dy = fin(lt, 0.04 * D)
    o = T(title, fit_size(title, 104), TH['fg'], 540, 500, op, dy)
    return o + render_list(lt, D, s['items'][:5], s.get('variant') or 'boxes', 760)


# ---- lens ----------------------------------------------------------------------------------------
def sc_lens(lt, D, s):
    if s.get('variant') == 'spotlight':
        return sc_lens_spot(lt, D, s)
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


def sc_lens_spot(lt, D, s):
    o = ''
    l1, l2 = s['line1'], s['line2']
    s1, s2 = fit_size(l1, 118), fit_size(l2, 132)
    op, dy = fin(lt, 0.04 * D)
    o += T(l1, s1, TH['fg'], 540, 760, op, dy)
    _, w2 = tp(l2, s2)
    be = ease((lt - 0.40 * D) / (0.16 * D))
    o += rrect(540 - w2 / 2 - 50, 880, w2 + 100, 200 * be, 36, fill=TH['ac'], op=0.18 * be)
    op, dy = fin(lt, 0.16 * D)
    o += T(l2, s2, TH['ac'], 540, 1010, op, dy)
    q = eio((lt - 0.22 * D) / (0.5 * D))
    la = clamp((lt - 0.2 * D) / 0.3) * clamp((0.93 * D - lt) / 0.3)
    if la > 0:
        lx, ly = 900 - 640 * q, 960
        o += (f'<g opacity="{la:.3f}"><circle cx="{lx:.1f}" cy="{ly:.1f}" r="120" fill="{TH["bg"]}" '
              f'fill-opacity="0.25" stroke="{TH["ac"]}" stroke-width="11"/>'
              f'<line x1="{lx + 84:.1f}" y1="{ly + 84:.1f}" x2="{lx + 150:.1f}" y2="{ly + 150:.1f}" '
              f'stroke="{TH["ac"]}" stroke-width="22" stroke-linecap="round"/></g>')
    g = ease((lt - 0.55 * D) / (0.2 * D))
    o += hline(840, 840 - 600 * g, 1240, TH['fg'], 3, 0.35)
    return o


# ---- stamp ---------------------------------------------------------------------------------------
def sc_stamp(lt, D, s):
    if s.get('variant') == 'seal':
        return sc_stamp_seal(lt, D, s)
    o = ''
    b = ease((lt - 0.04 * D) / (0.15 * D))
    o += card(540, 860, 600, 800, TH['pg'], b)
    o += doc_lines(540, 860, 600, 800, TH['ink'], 0.45 * b, 6)
    st = clamp((lt - 0.30 * D) / (0.14 * D))
    if st > 0:
        o += fp_group(1.0, 7.0 * (1.5 - 0.5 * ease(st)), 690, 1085, TH['st'], 1.3, ease(st))
        pr = ease((lt - 0.43 * D) / (0.18 * D))
        if 0 < pr < 1:
            o += circ(690, 1085, 130 + 110 * pr, stroke=TH['ac'], sw=5, op=(1 - pr) * 0.7)
    text = s['text']
    op, dy = fin(lt, 0.175 * D)
    o += T(text, fit_size(text, 118), TH['fg'], 540, 1530, op, dy)
    return o


def sc_stamp_seal(lt, D, s):
    o = ''
    cx, cy, R = 540, 840, 330
    pr = ease((lt - 0.05 * D) / (0.3 * D))
    C = 2 * math.pi * R
    press = ease((lt - 0.40 * D) / (0.14 * D))
    k = 1.0 + 0.30 * (1 - press) if lt > 0.40 * D else 1.0
    o += f'<g transform="translate({cx} {cy}) scale({k:.3f}) translate({-cx} {-cy})">'
    o += (f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" stroke="{TH["ac"]}" stroke-width="7" '
          f'stroke-dasharray="{C * pr:.1f} {C:.1f}" transform="rotate(-90 {cx} {cy})"/>')
    o += circ(cx, cy, R - 40, stroke=TH['ac'], sw=2, op=pr)
    o += fp_group(ease((lt - 0.15 * D) / (0.4 * D)), 11, cx, cy, TH['ac'], 1.0)
    o += '</g>'
    ring = ease((lt - 0.45 * D) / (0.2 * D))
    if 0 < ring < 1:
        o += circ(cx, cy, R + 20 + 120 * ring, stroke=TH['ac'], sw=4, op=(1 - ring) * 0.6)
    text = s['text']
    op, dy = fin(lt, 0.30 * D)
    o += T(text, fit_size(text, 112), TH['fg'], 540, 1420, op, dy)
    return o


# ---- question ------------------------------------------------------------------------------------
def sc_question(lt, D, s):
    v = s.get('variant') or 'bigmark'
    if v == 'bubble':
        return sc_question_bubble(lt, D, s)
    if v == 'ring':
        return sc_question_ring(lt, D, s)
    o = ''
    qa = ease((lt - 0.02 * D) / (0.25 * D))
    o += T('؟', 950, TH['ac'], 540, 1190, qa * 0.14, (1 - qa) * 60)
    lines = (s.get('lines') or wrap_lines(s['text'], 104))[:3]
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


def sc_question_bubble(lt, D, s):
    o = ''
    lines = (s.get('lines') or wrap_lines(s['text'], 88, 720))[:3]
    n = len(lines)
    h = n * 118 + 120
    y0 = 560
    b = ease((lt - 0.04 * D) / (0.16 * D))
    k = 0.9 + 0.1 * b
    o += f'<g transform="translate(540 {y0 + h / 2}) scale({k:.3f}) translate(-540 {-(y0 + h / 2)})" opacity="{b:.3f}">'
    o += (f'<polygon points="800,{y0 + h - 3} 900,{y0 + h + 100} 690,{y0 + h - 3}" fill="{TH["bg"]}" '
          f'stroke="{TH["ac"]}" stroke-width="5" stroke-linejoin="round"/>')
    o += rrect(110, y0, 860, h, 54, fill=TH['bg'], stroke=TH['ac'], sw=5)
    o += f'<rect x="700" y="{y0 + h - 8}" width="98" height="12" fill="{TH["bg"]}"/>'
    o += '</g>'
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.16 + 0.1 * i) * D)
        o += T(ln, fit_size(ln, 88, 720), TH['fg'], 540, y0 + 112 + i * 118, op, dy)
    if s.get('answer'):
        op, dy = fin(lt, 0.58 * D)
        o += pill_text(s['answer'], 540, y0 + h + 260, fit_size(s['answer'], 78, 640), True, op, dy)
    return o


def sc_question_ring(lt, D, s):
    o = ''
    R = 430
    C = 2 * math.pi * R
    pr = ease((lt - 0.04 * D) / (0.4 * D))
    o += (f'<circle cx="540" cy="900" r="{R}" fill="none" stroke="{TH["ac"]}" stroke-width="7" '
          f'stroke-dasharray="{C * pr:.1f} {C:.1f}" transform="rotate(-90 540 900)"/>')
    o += circ(540, 900, R - 36, stroke=TH['ac'], sw=2, op=pr * 0.5)
    lines = (s.get('lines') or wrap_lines(s['text'], 92, 600))[:4]
    n = len(lines)
    y0 = 900 - (n - 1) * 62 + 30
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.14 + 0.1 * i) * D)
        o += T(ln, fit_size(ln, 92, 600), TH['fg'], 540, y0 + i * 124, op, dy)
    if s.get('answer'):
        ans = s['answer']
        sz = fit_size(ans, 86)
        op, dy = fin(lt, 0.6 * D)
        o += T(ans, sz, TH['ac'], 540, 1560, op, dy)
        _, w = tp(ans, sz)
        g = ease((lt - 0.68 * D) / (0.16 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, 1594, TH['ac'], 6, g)
    return o


# ---- bignumber -----------------------------------------------------------------------------------
def sc_bignumber(lt, D, s):
    if s.get('variant') == 'dots':
        return sc_bignumber_dots(lt, D, s)
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


def sc_bignumber_dots(lt, D, s):
    o = ''
    val = max(1, min(int(s['value']), 12))
    if s.get('label'):
        op, dy = fin(lt, 0.02 * D)
        o += T(s['label'], fit_size(s['label'], 76), TH['fg'], 540, 560, op, dy)
    big = str(val).translate(AR_DIGITS)
    op = ease((lt - 0.04 * D) / (0.12 * D))
    o += T(big, 300, TH['ac'], 540, 860, op)
    cols = 4 if val > 6 else 3
    rows = (val + cols - 1) // cols
    gx, gy = 170, 150
    x0 = 540 - (cols - 1) * gx / 2
    y0 = 1010
    for i in range(val):
        r_, c_ = divmod(i, cols)
        x = x0 + c_ * gx
        y = y0 + r_ * gy
        t0 = (0.18 + 0.45 * i / max(val - 1, 1)) * D
        pr = ease((lt - t0) / 0.4)
        o += circ(x, y, 46, stroke=TH['fg'], sw=3, op=0.25)
        if pr > 0:
            o += circ(x, y, 46 * (0.5 + 0.5 * pr), fill=TH['ac'], op=pr)
    if s.get('caption'):
        yc = y0 + rows * gy + 60
        for i, ln in enumerate(wrap_lines(s['caption'], 76)[:2]):
            op, dy = fin(lt, (0.7 + 0.08 * i) * D)
            o += T(ln, fit_size(ln, 76), TH['fg'], 540, yc + i * 100, op, dy)
    return o


# ---- versus --------------------------------------------------------------------------------------
def _single(s):
    return len(s['a']['points']) == 1 and len(s['b']['points']) == 1


def sc_versus(lt, D, s):
    v = s.get('variant')
    if v == 'flip' and _single(s):
        return sc_versus_flip(lt, D, s)
    if v == 'stack' or (v is None and _single(s)):
        if _single(s):
            return sc_versus_stack(lt, D, s)
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
        o += rrect(105, 545, 430, 970, 30, stroke=TH['ac'], sw=4, op=hl)
    return o


def sc_versus_stack(lt, D, s):
    """Myth on top (struck through), fact below in an outlined card. One point each."""
    o = ''
    a, b = s['a'], s['b']
    pa, pb = a['points'][0], b['points'][0]
    op, dy = fin(lt, 0.04 * D)
    la, lb = a['title'], b['title']
    wa = tp(la, 56)[1]
    o += rrect(540 - wa / 2 - 34, 425 + dy, wa + 68, 84, 42, stroke=TH['fg'], sw=3, op=op * 0.55)
    o += T(la, 56, TH['fg'], 540, 484, op * 0.7, dy)
    lines = wrap_lines(pa, 84, 840)[:3]
    for i, ln in enumerate(lines):
        op2, dy2 = fin(lt, (0.10 + 0.05 * i) * D)
        y = 640 + i * 112
        sz = fit_size(ln, 84, 840)
        o += T(ln, sz, TH['fg'], 540, y, op2 * 0.62, dy2)
        _, w = tp(ln, sz)
        g = ease((lt - (0.36 + 0.06 * i) * D) / (0.14 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, y - 26, TH['ac'], 7, g)
    g = ease((lt - 0.48 * D) / (0.12 * D))
    o += (f'<line x1="540" y1="920" x2="540" y2="{920 + 90 * g:.1f}" stroke="{TH["ac"]}" stroke-width="5" '
          f'stroke-linecap="round" opacity="{g * 0.8:.3f}"/>')
    op, dy = fin(lt, 0.56 * D)
    wb = tp(lb, 56)[1]
    o += rrect(540 - wb / 2 - 34, 1050 + dy, wb + 68, 84, 42, fill=TH['ac'], op=op)
    o += T(lb, 56, TH['bg'], 540, 1109, op, dy)
    flines = wrap_lines(pb, 96, 800)[:3]
    ch = len(flines) * 124 + 90
    cb = ease((lt - 0.6 * D) / (0.14 * D))
    o += rrect(90, 1170, 900, ch, 36, stroke=TH['ac'], sw=5, op=cb)
    for i, ln in enumerate(flines):
        op3, dy3 = fin(lt, (0.64 + 0.06 * i) * D)
        o += T(ln, fit_size(ln, 96, 800), TH['fg'], 540, 1170 + 105 + i * 124, op3, dy3)
    return o


def sc_versus_flip(lt, D, s):
    """One card: shows the myth, flips, and shows the fact."""
    o = ''
    a, b = s['a'], s['b']
    cx, cy, w, h = 540, 940, 880, 800
    show = ease((lt - 0.04 * D) / (0.14 * D))
    prog = ease((lt - 0.42 * D) / (0.22 * D))
    sx = abs(math.cos(math.pi * prog))
    face_b = prog >= 0.5
    c = b if face_b else a
    text = c['points'][0]
    fill = TH['ac'] if face_b else 'none'
    tcol = TH['bg'] if face_b else TH['fg']
    o += (f'<g transform="translate({cx} {cy}) scale({max(sx, 0.001):.3f} 1) translate({-cx} {-cy})" '
          f'opacity="{show:.3f}">')
    o += rrect(cx - w / 2, cy - h / 2, w, h, 48, fill=fill, stroke=TH['ac'], sw=5)
    if face_b:
        _, wl = tp(c['title'], 58)
        o += rrect(cx - wl / 2 - 36, cy - h / 2 + 110 - 58 * 1.55 * 0.68, wl + 72, 58 * 1.55, 58 * 1.55 / 2, fill=TH['bg'])
        o += T(c['title'], 58, TH['ac'], cx, cy - h / 2 + 110)
    else:
        o += pill_text(c['title'], cx, cy - h / 2 + 110, 58, True)
    lines = wrap_lines(text, 84, 700)[:4]
    y0 = cy - (len(lines) - 1) * 56 + 40
    for i, ln in enumerate(lines):
        o += T(ln, fit_size(ln, 84, 700), tcol if face_b else TH['fg'], cx, y0 + i * 112, 1.0 if face_b else 0.85)
    o += '</g>'
    return o


# ---- quote ---------------------------------------------------------------------------------------
def sc_quote(lt, D, s):
    v = s.get('variant') or 'bar'
    if v == 'marks':
        return sc_quote_marks(lt, D, s)
    if v == 'card':
        return sc_quote_card(lt, D, s)
    o = ''
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.02 * D)
        sz = fit_size(by, 54, 600)
        _, w = tp(by, sz)
        o += rrect(540 - w / 2 - 36, 472 + dy, w + 72, 92, 46, stroke=TH['ac'], sw=3, op=op)
        o += T(by, sz, TH['ac'], 540, 535, op, dy)
    lines = wrap_lines(s['text'], 92, 740)[:5]
    n = len(lines)
    g = ease((lt - 0.05 * D) / (0.3 * D))
    h = n * 135 + 20
    o += (f'<line x1="930" y1="760" x2="930" y2="{760 + h * g:.1f}" stroke="{TH["ac"]}" '
          f'stroke-width="10" stroke-linecap="round"/>')
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.12 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 92, 760), TH['fg'], 880, 850 + i * 135, op, dy, 'end')
    return o


def _qmark(x, y, op):
    s = ''
    for dx in (0, 70):
        s += (f'<g transform="translate({x + dx} {y}) rotate(14)"><rect x="-13" y="-48" width="26" height="96" rx="13" '
              f'fill="{TH["ac"]}" opacity="{op:.3f}"/></g>')
    return s


def sc_quote_marks(lt, D, s):
    o = ''
    a = ease((lt - 0.02 * D) / (0.14 * D))
    o += _qmark(505, 640 + (1 - a) * 30, a)
    lines = wrap_lines(s['text'], 90, 800)[:5]
    n = len(lines)
    y0 = 860 - (n - 1) * 5
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.14 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 90, 800), TH['fg'], 540, y0 + i * 130, op, dy)
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.7 * D)
        o += pill_text(by, 540, y0 + n * 130 + 90, fit_size(by, 52, 560), False, op, dy)
    return o


def sc_quote_card(lt, D, s):
    o = ''
    lines = wrap_lines(s['text'], 88, 740)[:5]
    n = len(lines)
    h = n * 128 + 150
    y0 = 960 - h / 2
    b = ease((lt - 0.03 * D) / (0.16 * D))
    o += rrect(90, y0, 900, h * b, 40, stroke=TH['ac'], sw=5, op=b)
    by = s.get('by')
    if by:
        sz = fit_size(by, 52, 520)
        _, w = tp(by, sz)
        op, dy = fin(lt, 0.08 * D)
        o += rrect(930 - w - 64, y0 - 46, w + 64, 92, 46, fill=TH['ac'], op=op)
        o += T(by, sz, TH['bg'], 930 - w / 2 - 32, y0 + 16, op, dy)
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.16 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 88, 780), TH['fg'], 540, y0 + 130 + i * 128, op, dy)
    return o


# ---- outro ---------------------------------------------------------------------------------------
def sc_outro(lt, D, s):
    v = s.get('variant') or 'center'
    name, slogan, cta = s.get('name', 'بصمة'), s.get('slogan', 'بصمتك على بحثك'), s.get('cta', 'راسلنا هسه')
    latin = s.get('latin', 'busma')
    o = ''
    if v == 'minimal':
        b = ease((lt - 0.15) / 0.7)
        sc = 2.6 * (0.9 + 0.1 * b)
        o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {470 - 50 * sc:.1f}) scale({sc:.3f})">'
              f'{mark_group(TH["wm"])}</g>')
        op, dy = fin(lt, 0.6)
        o += T(name, 280, TH['fg'], 540, 1010, op, dy)
        if latin:
            op, dy = fin(lt, 0.9)
            o += T(latin, 62, TH['ac'], 540, 1110, op * 0.9, dy, font=LA500, tracking=0.34)
        op, dy = fin(lt, 1.2)
        o += T(slogan, 64, TH['fg'], 540, 1250, op * 0.85, dy, font=AR400)
        op, dy = fin(lt, 1.6)
        sz = fit_size(cta, 76, 700)
        _, w = tp(cta, sz)
        o += T(cta, sz, TH['ac'], 540, 1500, op, dy)
        g = ease((lt - 1.8) / 0.5)
        o += hline(540 + w / 2, 540 + w / 2 - w * g, 1540, TH['ac'], 6, g)
        return o
    if v == 'row':
        b = ease((lt - 0.15) / 0.7)
        sc = 3.7 * (0.92 + 0.08 * b)
        o += f'<g opacity="{b:.3f}" transform="translate(600 {760 - 50 * sc + 50:.1f}) scale({sc:.3f})">{mark_group(TH["wm"])}</g>'
        op, dy = fin(lt, 0.6)
        o += T(name, 170, TH['fg'], 560, 850, op, dy, 'end')
        if latin:
            op, dy = fin(lt, 0.85)
            o += T(latin, 46, TH['ac'], 556, 925, op * 0.9, dy, 'end', font=LA500, tracking=0.3)
        op, dy = fin(lt, 1.1)
        o += T(slogan, 52, TH['fg'], 560, 1010, op * 0.85, dy, 'end', font=AR400)
        op, dy = fin(lt, 1.6)
        if op > 0:
            o += rrect(300, 1280 + dy, 480, 116, 58, stroke=TH['ac'], sw=4, op=op)
            o += T(cta, fit_size(cta, 62, 400), TH['fg'], 540, 1357, op, dy)
        return o
    b = ease((lt - 0.15) / 0.7)
    sc = 5.6 * (0.92 + 0.08 * b)
    o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {740 - 50 * sc:.1f}) '
          f'scale({sc:.3f})">{mark_group(TH["wm"])}</g>')
    op, dy = fin(lt, 0.7)
    o += T(name, 210, TH['fg'], 540, 1225, op, dy)
    if latin:
        op, dy = fin(lt, 0.95)
        o += T(latin, 58, TH['fg'], 540, 1312, op * 0.75, dy, font=LA500, tracking=0.32)
    op, dy = fin(lt, 1.25)
    o += T(slogan, 64, TH['ac'], 540, 1415, op, dy, font=AR400)
    op, dy = fin(lt, 1.7)
    if op > 0:
        o += rrect(300, 1525 + dy, 480, 116, 58, stroke=TH['ac'], sw=4, op=op)
        o += T(cta, fit_size(cta, 62, 400), TH['fg'], 540, 1602, op, dy)
    return o


SCENE_TYPES = {'hook': sc_hook, 'contrast': sc_contrast, 'steps': sc_steps, 'lens': sc_lens,
               'stamp': sc_stamp, 'question': sc_question, 'bignumber': sc_bignumber,
               'checklist': sc_checklist, 'versus': sc_versus, 'quote': sc_quote, 'outro': sc_outro}


def allowed_variants(s):
    t = s['type']
    v = list(VARIANTS.get(t, []))
    if t == 'versus':
        v = ['stack', 'flip'] if _single(s) else ['columns']
    if t == 'bignumber':
        try:
            if not 1 <= int(s['value']) <= 12:
                v = ['count']
        except Exception:
            v = ['count']
    return v


# ================================================================ extended design library (v2)
THEMES.update({
    'slate': dict(bg='#1B2B40', fg=IV,   ac=CH,       ln=CH,   pg=IV,   ink=NAVY, st=NAVY, wm=CH),
    'cream': dict(bg='#FBF7EE', fg=NAVY, ac=GOLD_M,   ln=NAVY, pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
    'gold':  dict(bg='#CDB27A', fg=NAVY, ac='#2F3E5C', ln=NAVY, pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
    'mist':  dict(bg='#DCE3EA', fg=NAVY, ac=GOLD_M,   ln=NAVY, pg=NAVY, ink=IV,   st=CH,   wm=NAVY),
})
FRAMES += ['brackets', 'dashed', 'diamond', 'inset', 'tab', 'underline']
TRANSITIONS += ['slidedown', 'flip', 'tilt', 'iris']
BACKGROUNDS += ['stripes', 'bokeh', 'halftone', 'ridges', 'plus', 'arcs', 'blocks', 'steps']
REVEALS += ['drop', 'zoomout', 'stretch']
DECORS = ['none', 'progress', 'counter', 'corners', 'orbit', 'ticks', 'edgedots', 'crosses', 'bar', 'pulse', 'tag']
VARIANTS['hook'] += ['stairs', 'ribbon', 'ring']
VARIANTS['contrast'] += ['flip', 'wipe']
VARIANTS['steps'] += ['zigzag', 'stairs', 'badges']
VARIANTS['checklist'] += ['zigzag', 'stairs', 'badges']
VARIANTS['lens'] += ['highlight', 'reveal']
VARIANTS['stamp'] += ['ribbon', 'badge']
VARIANTS['question'] += ['typed', 'card']
VARIANTS['bignumber'] += ['bars', 'ticks']
VARIANTS['versus'] = ['columns', 'cardcols', 'stack', 'flip', 'tabs', 'bands']
VARIANTS['quote'] += ['banner', 'underline']
VARIANTS['outro'] += ['filled', 'framed']
VARIANT_FUNCS = {}
EXTRA_LISTS = {}


def reg(t, v):
    def deco(f):
        VARIANT_FUNCS[(t, v)] = f
        return f
    return deco


def _lines_of(s, size, maxw, n=3):
    text = s.get('text') or ' '.join(s.get('lines', []))
    return (s.get('lines') or wrap_lines(text, size, maxw))[:n]


# ---------------------------------------------------------------- extra list styles
def list_zigzag(lt, D, items, ytop):
    o, n = '', len(items)
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.5)
        if pr <= 0:
            continue
        right = (i % 2 == 0)
        x0 = 330 if right else 90
        y = ytop + i * 170
        cxn = x0 + 600
        dx = (1 - pr) * (260 if right else -260)
        g = f'<g transform="translate({dx:.1f} 0)" opacity="{pr:.3f}">'
        g += rrect(x0, y - 66, 660, 132, 30, stroke=TH['ac'], sw=4)
        g += circ(cxn, y, 40, fill=TH['ac'])
        g += T(numeral(i + 1), 50, TH['bg'], cxn, y + 17)
        g += T(label, fit_size(label, 66, 480), TH['fg'], cxn - 62, y + 22, 1, 0, 'end')
        o += g + '</g>'
    return o


def list_stairs(lt, D, items, ytop):
    o, n = '', len(items)
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.5)
        if pr <= 0:
            continue
        w = 520 + i * 100
        y = ytop + i * 150
        o += rrect(990 - w * pr, y - 58, w * pr, 116, 58, fill=TH['ac'])
        if pr > 0.5:
            o += T(label, fit_size(label, 64, w - 130), TH['bg'], 990 - 56, y + 21, ease((pr - 0.5) * 2), 0, 'end')
    return o


def list_badges(lt, D, items, ytop):
    o, n = '', len(items)
    X = 840
    for i in range(n - 1):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st - 0.1 * D) / (0.12 * D))
        if pr > 0:
            y1, y2 = ytop + i * 160 + 56, ytop + (i + 1) * 160 - 56
            o += (f'<line x1="{X}" y1="{y1}" x2="{X}" y2="{y1 + (y2 - y1) * pr:.1f}" stroke="{TH["ac"]}" stroke-width="5" '
                  f'stroke-dasharray="4 12" stroke-linecap="round"/>')
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.5)
        if pr <= 0:
            continue
        y = ytop + i * 160
        o += circ(X, y, 54 * (0.7 + 0.3 * pr), fill=TH['bg'], stroke=TH['ac'], sw=5, op=pr)
        o += T(numeral(i + 1), 60, TH['ac'], X, y + 21, pr)
        op, dy = fin(lt, st + 0.1)
        o += T(label, fit_size(label, 76, 600), TH['fg'], X - 92, y + 26, op, dy, 'end')
    return o


EXTRA_LISTS.update({'zigzag': list_zigzag, 'stairs': list_stairs, 'badges': list_badges})


# ---------------------------------------------------------------- hook variants
@reg('hook', 'stairs')
def sc_hook_stairs(lt, D, s):
    o = fp_group(ease((lt - 0.08 * D) / (0.6 * D)), 7.5, 250, 1500, TH['ac'], 1.1)
    lines = _lines_of(s, 92, 700, 4)
    n = len(lines)
    g = ease((lt - 0.05 * D) / (0.3 * D))
    o += (f'<line x1="1000" y1="650" x2="1000" y2="{650 + (n * 150 + 20) * g:.1f}" stroke="{TH["ac"]}" '
          f'stroke-width="10" stroke-linecap="round"/>')
    for i, ln in enumerate(lines):
        xe = 940 - i * 120
        op, dy = fin(lt, (0.12 + 0.16 * i) * D)
        o += T(ln, fit_size(ln, 96, xe - 80), TH['fg'] if i % 2 == 0 else TH['ac'], xe, 760 + i * 150, op, dy, 'end')
    return o


@reg('hook', 'ribbon')
def sc_hook_ribbon(lt, D, s):
    lines = _lines_of(s, 100, 860, 3)
    o = fp_group(ease((lt - 0.05 * D) / (0.5 * D)), 6.0, 540, 540, TH['ac'], 1.0)
    p = ease((lt - 0.12 * D) / (0.2 * D))
    o += '<g transform="rotate(-7 540 960)">'
    o += f'<rect x="{540 - 760 * p:.1f}" y="840" width="{1520 * p:.1f}" height="260" fill="{TH["ac"]}"/>'
    first = lines[0]
    op, dy = fin(lt, 0.30 * D)
    o += T(first, fit_size(first, 104, 860), TH['bg'], 540, 1000, op, dy)
    for i, ln in enumerate(lines[1:3]):
        op, dy = fin(lt, (0.44 + 0.14 * i) * D)
        o += T(ln, fit_size(ln, 96, 860), TH['fg'], 540, 1260 + i * 130, op, dy)
    o += '</g>'
    return o


@reg('hook', 'ring')
def sc_hook_ring(lt, D, s):
    R = 400
    C = 2 * math.pi * R
    pr = ease((lt - 0.04 * D) / (0.4 * D))
    o = (f'<circle cx="540" cy="900" r="{R}" fill="none" stroke="{TH["ac"]}" stroke-width="7" '
         f'stroke-dasharray="{C * pr:.1f} {C:.1f}" transform="rotate(-90 540 900)"/>')
    o += circ(540, 900, R - 34, stroke=TH['ac'], sw=2, op=pr * 0.5)
    lines = _lines_of(s, 90, 560, 4)
    n = len(lines)
    y0 = 900 - (n - 1) * 60 + 28
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.18 + 0.12 * i) * D)
        o += T(ln, fit_size(ln, 90, 560), TH['fg'] if i % 2 == 0 else TH['ac'], 540, y0 + i * 120, op, dy)
    o += fp_group(ease((lt - 0.3 * D) / (0.5 * D)), 4.5, 540, 1480, TH['ac'], 1.0)
    return o


# ---------------------------------------------------------------- contrast variants
def flip_card(lt, D, la, ta, lb, tb, t0=0.42):
    o = ''
    cx, cy, w, h = 540, 940, 880, 760
    show = ease((lt - 0.04 * D) / (0.14 * D))
    prog = ease((lt - t0 * D) / (0.22 * D))
    sx = abs(math.cos(math.pi * prog))
    face_b = prog >= 0.5
    label, text = (lb, tb) if face_b else (la, ta)
    o += (f'<g transform="translate({cx} {cy}) scale({max(sx, 0.001):.3f} 1) translate({-cx} {-cy})" opacity="{show:.3f}">')
    o += rrect(cx - w / 2, cy - h / 2, w, h, 48, fill=TH['ac'] if face_b else 'none', stroke=TH['ac'], sw=5)
    if face_b:
        _, wl = tp(label, 58)
        o += rrect(cx - wl / 2 - 36, cy - h / 2 + 110 - 58 * 1.55 * 0.68, wl + 72, 58 * 1.55, 58 * 1.55 / 2, fill=TH['bg'])
        o += T(label, 58, TH['ac'], cx, cy - h / 2 + 110)
    else:
        o += pill_text(label, cx, cy - h / 2 + 110, 58, True)
    lines = wrap_lines(text, 100, 720)[:3]
    y0 = cy - (len(lines) - 1) * 62 + 50
    for i, ln in enumerate(lines):
        o += T(ln, fit_size(ln, 100, 720), TH['bg'] if face_b else TH['fg'], cx, y0 + i * 124, 1.0 if face_b else 0.85)
    return o + '</g>'


@reg('contrast', 'flip')
def sc_contrast_flip(lt, D, s):
    return flip_card(lt, D, s.get('before_label', 'قبل'), s['before'], s.get('after_label', 'بعد'), s['after'])


@reg('contrast', 'wipe')
def sc_contrast_wipe(lt, D, s):
    o = ''
    before, after = s['before'], s['after']
    bs, as_ = fit_size(before, 140, 860), fit_size(after, 140, 860)
    p = eio((lt - 0.42 * D) / (0.26 * D))
    op, dy = fin(lt, 0.05 * D)
    o += T(before, bs, TH['fg'], 540, 960, op * (0.8 - 0.65 * p), dy)
    _, wb = tp(before, bs)
    g = ease((lt - 0.26 * D) / (0.12 * D))
    o += hline(540 + wb / 2, 540 + wb / 2 - wb * g, 922, TH['ac'], 8, g * (1 - p))
    if p > 0:
        xcut = 1080 * (1 - p)
        o += (f'<clipPath id="cw"><rect x="{xcut:.1f}" y="700" width="{1080 - xcut:.1f}" height="520"/></clipPath>'
              f'<g clip-path="url(#cw)">{T(after, as_, TH["ac"], 540, 960)}</g>')
        o += f'<line x1="{xcut:.1f}" y1="760" x2="{xcut:.1f}" y2="1160" stroke="{TH["ac"]}" stroke-width="8" stroke-linecap="round"/>'
    return o


# ---------------------------------------------------------------- lens variants
@reg('lens', 'highlight')
def sc_lens_highlight(lt, D, s):
    o = ''
    l1, l2 = s['line1'], s['line2']
    s1, s2 = fit_size(l1, 118), fit_size(l2, 136)
    op, dy = fin(lt, 0.04 * D)
    o += T(l1, s1, TH['fg'], 540, 760, op, dy)
    _, w2 = tp(l2, s2)
    p = eio((lt - 0.30 * D) / (0.4 * D))
    xr = 540 + w2 / 2 + 30
    if p > 0:
        o += rrect(xr - (w2 + 60) * p, 900, (w2 + 60) * p, 150, 20, fill=TH['ac'], op=0.35)
    op, dy = fin(lt, 0.16 * D)
    o += T(l2, s2, TH['ac'], 540, 1010, op, dy)
    la = clamp((lt - 0.28 * D) / 0.3) * clamp((0.93 * D - lt) / 0.3)
    if la > 0:
        lx = xr - (w2 + 60) * p
        o += (f'<g opacity="{la:.3f}"><circle cx="{lx:.1f}" cy="975" r="86" fill="{TH["bg"]}" fill-opacity="0.2" '
              f'stroke="{TH["ac"]}" stroke-width="9"/><line x1="{lx + 60:.1f}" y1="1035" x2="{lx + 112:.1f}" y2="1087" '
              f'stroke="{TH["ac"]}" stroke-width="18" stroke-linecap="round"/></g>')
    return o


@reg('lens', 'reveal')
def sc_lens_reveal(lt, D, s):
    o = ''
    l1, l2 = s['line1'], s['line2']
    s2 = fit_size(l2, 140)
    op, dy = fin(lt, 0.04 * D)
    o += T(l1, fit_size(l1, 118), TH['fg'], 540, 700, op, dy)
    q = eio((lt - 0.20 * D) / (0.5 * D))
    o += T(l2, s2, TH['fg'], 540, 1010, 0.10 * ease((lt - 0.1 * D) / 0.3))
    if q > 0:
        _, w2 = tp(l2, s2)
        lx = 540 + w2 / 2 + 60 - (w2 + 120) * q
        r = 150 if q < 0.999 else 150
        grow = ease((lt - 0.72 * D) / (0.2 * D))
        rr = 150 + 1000 * grow
        o += (f'<clipPath id="lr"><circle cx="{lx:.1f}" cy="975" r="{rr:.1f}"/></clipPath>'
              f'<g clip-path="url(#lr)">{T(l2, s2, TH["ac"], 540, 1010)}</g>')
        if grow < 0.98:
            la = clamp((lt - 0.2 * D) / 0.3) * (1 - grow)
            o += (f'<g opacity="{la:.3f}"><circle cx="{lx:.1f}" cy="975" r="150" fill="none" stroke="{TH["ac"]}" stroke-width="10"/>'
                  f'<line x1="{lx + 106:.1f}" y1="1081" x2="{lx + 170:.1f}" y2="1145" stroke="{TH["ac"]}" stroke-width="20" '
                  f'stroke-linecap="round"/></g>')
    return o


# ---------------------------------------------------------------- stamp variants
@reg('stamp', 'ribbon')
def sc_stamp_ribbon(lt, D, s):
    o = ''
    text = s['text']
    st = clamp((lt - 0.40 * D) / (0.16 * D))
    o += fp_group(ease((lt - 0.12 * D) / (0.4 * D)), 8.5, 540, 620, TH['ac'], 1.1)
    p = ease((lt - 0.08 * D) / (0.2 * D))
    o += '<g transform="rotate(-6 540 1100)">'
    o += f'<rect x="{540 - 780 * p:.1f}" y="980" width="{1560 * p:.1f}" height="250" fill="{TH["ac"]}"/>'
    op, dy = fin(lt, 0.24 * D)
    o += T(text, fit_size(text, 112, 860), TH['bg'], 540, 1140, op, dy)
    o += '</g>'
    if st > 0:
        pr = ease((lt - 0.52 * D) / (0.18 * D))
        if 0 < pr < 1:
            o += circ(540, 620, 200 + 160 * pr, stroke=TH['ac'], sw=4, op=(1 - pr) * 0.6)
    return o


@reg('stamp', 'badge')
def sc_stamp_badge(lt, D, s):
    o = ''
    R = 340
    pts = [(540 + R * math.cos(math.radians(-90 + 60 * k)), 840 + R * math.sin(math.radians(-90 + 60 * k))) for k in range(6)]
    d = 'M ' + ' L '.join(f'{x:.1f} {y:.1f}' for x, y in pts) + ' Z'
    per = 6 * R
    pr = ease((lt - 0.05 * D) / (0.34 * D))
    press = ease((lt - 0.42 * D) / (0.14 * D))
    k = 1.0 + 0.25 * (1 - press) if lt > 0.42 * D else 1.0
    o += f'<g transform="translate(540 840) scale({k:.3f}) translate(-540 -840)">'
    o += (f'<path d="{d}" fill="none" stroke="{TH["ac"]}" stroke-width="8" stroke-linejoin="round" '
          f'stroke-dasharray="{per * pr:.1f} {per:.1f}"/>')
    o += fp_group(ease((lt - 0.15 * D) / (0.4 * D)), 10, 540, 840, TH['ac'], 1.0)
    o += '</g>'
    text = s['text']
    op, dy = fin(lt, 0.30 * D)
    o += T(text, fit_size(text, 112), TH['fg'], 540, 1400, op, dy)
    return o


# ---------------------------------------------------------------- question variants
def type_lines(lt, D, lines, size, y0, gap, start, span, cols):
    o, n = '', len(lines)
    p = clamp((lt - start * D) / (span * D))
    for i, ln in enumerate(lines):
        pi = clamp(p * n - i)
        if pi <= 0:
            continue
        sz = fit_size(ln, size, 860)
        d, w = tp(ln, sz)
        y = y0 + i * gap
        xr = 540 + w / 2
        x0 = xr - w * pi - 6
        o += (f'<clipPath id="tq{i}"><rect x="{x0:.1f}" y="{y - sz:.1f}" width="{w * pi + 12:.1f}" height="{sz * 1.7:.1f}"/></clipPath>'
              f'<g clip-path="url(#tq{i})"><path d="{d}" transform="translate(540 {y})" fill="{cols[i % len(cols)]}"/></g>')
        if pi < 1 or i == n - 1:
            blink = 1.0 if pi < 1 else (0.5 + 0.5 * math.sin(lt * 9))
            o += (f'<line x1="{xr - w * pi:.1f}" y1="{y - sz * 0.8:.1f}" x2="{xr - w * pi:.1f}" y2="{y + sz * 0.25:.1f}" '
                  f'stroke="{TH["ac"]}" stroke-width="7" stroke-linecap="round" opacity="{blink:.3f}"/>')
    return o


@reg('question', 'typed')
def sc_question_typed(lt, D, s):
    lines = _lines_of(s, 104, 860, 3)
    o = T('؟', 300, TH['ac'], 540, 560, ease((lt - 0.02 * D) / (0.2 * D)) * 0.9)
    o += type_lines(lt, D, lines, 104, 860, 140, 0.12, 0.5, [TH['fg']])
    if s.get('answer'):
        op, dy = fin(lt, 0.66 * D)
        o += pill_text(s['answer'], 540, 860 + len(lines) * 140 + 150, fit_size(s['answer'], 80, 700), True, op, dy)
    return o


@reg('question', 'card')
def sc_question_card(lt, D, s):
    o = ''
    lines = _lines_of(s, 90, 720, 3)
    a = ease((lt - 0.04 * D) / (0.16 * D))
    o += rrect(110, 560, 860, 380 * a, 44, stroke=TH['fg'], sw=4, op=a * 0.6)
    n = len(lines)
    y0 = 750 - (n - 1) * 52 + 24
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.14 + 0.1 * i) * D)
        o += T(ln, fit_size(ln, 90, 720), TH['fg'], 540, y0 + i * 112, op, dy)
    ar = ease((lt - 0.46 * D) / (0.1 * D))
    o += (f'<polyline points="500,990 540,1030 580,990" fill="none" stroke="{TH["ac"]}" stroke-width="9" '
          f'stroke-linecap="round" stroke-linejoin="round" opacity="{ar:.3f}"/>')
    if s.get('answer'):
        b = ease((lt - 0.52 * D) / (0.16 * D))
        k = 0.9 + 0.1 * b
        ans = s['answer']
        o += (f'<g transform="translate(540 1230) scale({k:.3f}) translate(-540 -1230)" opacity="{b:.3f}">'
              + rrect(110, 1090, 860, 280, 44, fill=TH['ac']) + T(ans, fit_size(ans, 100, 740), TH['bg'], 540, 1255) + '</g>')
    return o


# ---------------------------------------------------------------- bignumber variants
@reg('bignumber', 'bars')
def sc_bignumber_bars(lt, D, s):
    o = ''
    val = max(1, min(int(s['value']), 10))
    if s.get('label'):
        op, dy = fin(lt, 0.02 * D)
        o += T(s['label'], fit_size(s['label'], 76), TH['fg'], 540, 560, op, dy)
    op = ease((lt - 0.04 * D) / (0.12 * D))
    o += T(str(val).translate(AR_DIGITS), 280, TH['ac'], 540, 840, op)
    bw, gap = 62, 22
    total = val * (bw + gap) - gap
    x0 = 540 - total / 2
    for i in range(val):
        t0 = (0.16 + 0.5 * i / max(val - 1, 1)) * D
        pr = ease((lt - t0) / 0.45)
        h = (120 + i * (380 / max(val - 1, 1))) * pr
        o += rrect(x0 + i * (bw + gap), 1500 - h, bw, h, 14, fill=TH['ac'])
    o += hline(x0 - 20, x0 + total + 20, 1514, TH['fg'], 4, 0.4)
    if s.get('caption'):
        op, dy = fin(lt, 0.7 * D)
        o += T(s['caption'], fit_size(s['caption'], 72, 860), TH['fg'], 540, 1640, op, dy)
    return o


@reg('bignumber', 'ticks')
def sc_bignumber_ticks(lt, D, s):
    o = ''
    val = max(1, min(int(s['value']), 12))
    if s.get('label'):
        op, dy = fin(lt, 0.02 * D)
        o += T(s['label'], fit_size(s['label'], 76), TH['fg'], 540, 540, op, dy)
    for k in range(12):
        ang = math.radians(-90 + 30 * k)
        lit = k < val
        t0 = (0.14 + 0.5 * k / 11) * D
        pr = ease((lt - t0) / 0.35) if lit else 1.0
        r1, r2 = 290, 290 + 74
        x1, y1 = 540 + r1 * math.cos(ang), 1000 + r1 * math.sin(ang)
        x2, y2 = 540 + r2 * math.cos(ang), 1000 + r2 * math.sin(ang)
        col = TH['ac'] if (lit and pr > 0.3) else TH['fg']
        opq = (0.35 + 0.65 * pr) if lit else 0.2
        o += (f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{col}" stroke-width="16" '
              f'stroke-linecap="round" opacity="{opq:.3f}"/>')
    op = ease((lt - 0.04 * D) / (0.12 * D))
    o += T(str(val).translate(AR_DIGITS), 320, TH['ac'], 540, 1110, op)
    if s.get('caption'):
        op, dy = fin(lt, 0.7 * D)
        o += T(s['caption'], fit_size(s['caption'], 72, 860), TH['fg'], 540, 1560, op, dy)
    return o


# ---------------------------------------------------------------- versus variants
@reg('versus', 'tabs')
def sc_versus_tabs(lt, D, s):
    o = ''
    a, b = s['a'], s['b']
    ta, tb = a['title'], b['title']
    prog = ease((lt - 0.46 * D) / (0.12 * D))
    appear = ease((lt - 0.04 * D) / (0.12 * D))
    xa, xb, y = 720, 360, 600
    for x, t_, active in ((xa, ta, 1 - prog), (xb, tb, prog)):
        o += T(t_, 70, TH['fg'] if t_ == ta else TH['ac'], x, y, appear * (0.45 + 0.55 * active))
    ux = xa + (xb - xa) * prog
    o += hline(ux - 120, ux + 120, y + 36, TH['ac'], 10, appear)
    o += hline(240, 840, y + 36, TH['fg'], 3, 0.25 * appear)
    card_a = ease((lt - 0.08 * D) / (0.16 * D))
    o += rrect(110, 740, 860, 640 * card_a, 44, stroke=TH['ac'], sw=5, op=card_a)
    for txt, op_, col in ((a['points'][0], 1 - prog, TH['fg']), (b['points'][0], prog, TH['fg'])):
        lines = wrap_lines(txt, 90, 740)[:4]
        y0 = 1060 - (len(lines) - 1) * 55
        for i, ln in enumerate(lines):
            o += T(ln, fit_size(ln, 90, 740), col, 540, y0 + i * 112, op_ * ease((lt - 0.14 * D) / (0.14 * D)))
    return o


@reg('versus', 'bands')
def sc_versus_bands(lt, D, s):
    o = ''
    a, b = s['a'], s['b']
    pa, pb = a['points'][0], b['points'][0]
    p1 = ease((lt - 0.05 * D) / (0.16 * D))
    o += f'<rect x="{1080 - 1080 * p1:.1f}" y="540" width="{1080 * p1:.1f}" height="400" fill="{TH["fg"]}" opacity="0.08"/>'
    o += T(a['title'], 50, TH['fg'], 960, 612, p1 * 0.7, 0, 'end')
    lines = wrap_lines(pa, 74, 820)[:3]
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.12 + 0.06 * i) * D)
        y = 730 + i * 92
        sz = fit_size(ln, 74, 820)
        o += T(ln, sz, TH['fg'], 540, y, op * 0.65, dy)
        _, w = tp(ln, sz)
        g = ease((lt - (0.34 + 0.06 * i) * D) / (0.14 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, y - 24, TH['ac'], 7, g)
    p2 = ease((lt - 0.52 * D) / (0.18 * D))
    o += f'<rect x="{1080 - 1080 * p2:.1f}" y="980" width="{1080 * p2:.1f}" height="470" fill="{TH["ac"]}"/>'
    op, dy = fin(lt, 0.58 * D)
    o += T(b['title'], 50, TH['bg'], 960, 1052, op * 0.85, dy, 'end')
    for i, ln in enumerate(wrap_lines(pb, 92, 800)[:3]):
        op, dy = fin(lt, (0.64 + 0.06 * i) * D)
        o += T(ln, fit_size(ln, 92, 800), TH['bg'], 540, 1190 + i * 112, op, dy)
    return o


@reg('versus', 'cardcols')
def sc_versus_cardcols(lt, D, s):
    o = ''
    for ci, (c, x0, t0, filled) in enumerate(((s['a'], 560, 0.06, False), (s['b'], 70, 0.34, True))):
        p = ease((lt - t0 * D) / (0.16 * D))
        dim = (1 - 0.35 * eio((lt - 0.6 * D) / (0.1 * D))) if ci == 0 else 1.0
        g = f'<g opacity="{p * dim:.3f}" transform="translate(0 {(1 - p) * 60:.1f})">'
        g += rrect(x0, 560, 450, 960, 36, fill=TH['ac'] if filled else 'none', stroke=TH['ac'], sw=5)
        tcol = TH['bg'] if filled else TH['fg']
        g += T(c['title'], fit_size(c['title'], 60, 360), tcol, x0 + 225, 660)
        g += hline(x0 + 70, x0 + 380, 700, tcol, 3, 0.5)
        for i, pnt in enumerate(c['points'][:3]):
            for li, ln in enumerate(wrap_lines(pnt, 46, 370)[:3]):
                g += T(ln, fit_size(ln, 46, 370), tcol, x0 + 225, 800 + i * 230 + li * 60)
        o += g + '</g>'
    return o


# ---------------------------------------------------------------- quote variants
@reg('quote', 'banner')
def sc_quote_banner(lt, D, s):
    o = ''
    by = s.get('by')
    lines = wrap_lines(s['text'], 92, 820)[:5]
    n = len(lines)
    h = n * 128 + 150
    y0 = 960 - h / 2
    p = ease((lt - 0.04 * D) / (0.18 * D))
    o += f'<rect x="{1080 - 1080 * p:.1f}" y="{y0:.1f}" width="{1080 * p:.1f}" height="{h}" fill="{TH["ac"]}"/>'
    if by:
        op, dy = fin(lt, 0.08 * D)
        o += pill_text(by, 540, y0 - 70, fit_size(by, 54, 560), False, op, dy)
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.2 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 92, 820), TH['bg'], 540, y0 + 112 + i * 128, op, dy)
    return o


@reg('quote', 'underline')
def sc_quote_underline(lt, D, s):
    o = ''
    lines = wrap_lines(s['text'], 92, 820)[:5]
    n = len(lines)
    y0 = 800 - (n - 1) * 8
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.10 + 0.5 * i / max(n, 1)) * D)
        sz = fit_size(ln, 92, 820)
        y = y0 + i * 140
        o += T(ln, sz, TH['fg'], 540, y, op, dy)
        _, w = tp(ln, sz)
        g = ease((lt - (0.20 + 0.5 * i / max(n, 1)) * D) / (0.14 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, y + 30, TH['ac'], 6, g * 0.9)
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.74 * D)
        o += pill_text(by, 540, y0 + n * 140 + 120, fit_size(by, 52, 560), True, op, dy)
    return o


# ---------------------------------------------------------------- outro variants
@reg('outro', 'filled')
def sc_outro_filled(lt, D, s):
    name, slogan, cta = s.get('name', 'بصمة'), s.get('slogan', 'بصمتك على بحثك'), s.get('cta', 'راسلنا هسه')
    latin = s.get('latin', 'busma')
    o = ''
    b = ease((lt - 0.15) / 0.7)
    sc = 4.6 * (0.92 + 0.08 * b)
    o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {600 - 50 * sc:.1f}) scale({sc:.3f})">'
          f'{mark_group(TH["wm"])}</g>')
    op, dy = fin(lt, 0.7)
    o += T(name, 230, TH['fg'], 540, 1110, op, dy)
    if latin:
        op, dy = fin(lt, 0.95)
        o += T(latin, 54, TH['ac'], 540, 1190, op, dy, font=LA500, tracking=0.34)
    op, dy = fin(lt, 1.25)
    o += T(slogan, 60, TH['fg'], 540, 1300, op * 0.85, dy, font=AR400)
    op, dy = fin(lt, 1.7)
    if op > 0:
        o += rrect(280, 1430 + dy, 520, 130, 65, fill=TH['ac'], op=op)
        o += T(cta, fit_size(cta, 68, 440), TH['bg'], 540, 1514, op, dy)
    return o


@reg('outro', 'framed')
def sc_outro_framed(lt, D, s):
    name, slogan, cta = s.get('name', 'بصمة'), s.get('slogan', 'بصمتك على بحثك'), s.get('cta', 'راسلنا هسه')
    latin = s.get('latin', 'busma')
    o = ''
    b = ease((lt - 0.15) / 0.7)
    sc = 3.2 * (0.92 + 0.08 * b)
    o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {470 - 50 * sc:.1f}) scale({sc:.3f})">'
          f'{mark_group(TH["wm"])}</g>')
    fr = ease((lt - 0.4) / 0.6)
    o += rrect(150, 760, 780, 380 * fr, 40, stroke=TH['ac'], sw=5, op=fr)
    op, dy = fin(lt, 0.8)
    o += T(name, 220, TH['fg'], 540, 980, op, dy)
    if latin:
        op, dy = fin(lt, 1.0)
        o += T(latin, 52, TH['ac'], 540, 1065, op, dy, font=LA500, tracking=0.34)
    op, dy = fin(lt, 1.3)
    o += T(slogan, 60, TH['fg'], 540, 1260, op * 0.85, dy, font=AR400)
    op, dy = fin(lt, 1.7)
    sz = fit_size(cta, 72, 700)
    _, w = tp(cta, sz)
    o += T(cta, sz, TH['ac'], 540, 1430, op, dy)
    g = ease((lt - 1.9) / 0.5)
    o += hline(540 + w / 2, 540 + w / 2 - w * g, 1468, TH['ac'], 6, g)
    return o


# ---------------------------------------------------------------- dispatcher + allowed variants
def scene_svg(s, lt):
    f = VARIANT_FUNCS.get((s['type'], s.get('variant')))
    return (f or SCENE_TYPES[s['type']])(lt, s['dur'], s)


def allowed_variants(s):
    t = s['type']
    v = list(VARIANTS.get(t, []))
    if t == 'versus':
        v = ['stack', 'flip', 'tabs', 'bands'] if _single(s) else ['columns', 'cardcols']
    if t == 'bignumber':
        try:
            if not 1 <= int(s['value']) <= 12:
                v = ['count']
            elif int(s['value']) > 10:
                v = [x for x in v if x != 'bars']
        except Exception:
            v = ['count']
    return v


# ---------------------------------------------------------------- extra backgrounds / frames / decor
def bg_extra(m, t):
    c = TH['ac']
    if m == 'stripes':
        off = (t * 12) % 180
        rs = ''.join(f'<rect x="{x - off:.1f}" y="0" width="90" height="{VH}"/>' for x in range(0, VW + 180, 180))
        return f'<g fill="{c}" opacity="0.05">{rs}</g>'
    if m == 'bokeh':
        out = ''
        for k in range(16):
            x = (k * 337) % 1100
            y0 = (k * 521) % 1900
            r = 40 + (k * 53) % 110
            y = (y0 - t * (6 + k % 5 * 2)) % 2000 - 40
            out += f'<circle cx="{x}" cy="{y:.1f}" r="{r}"/>'
        return f'<g fill="{c}" opacity="0.06">{out}</g>'
    if m == 'halftone':
        pts = ''
        for iy in range(0, 28):
            y = 90 + iy * 66
            r = 1.5 + 5.5 * (iy / 28)
            for ix in range(0, 17):
                pts += f'<circle cx="{40 + ix * 66}" cy="{y}" r="{r:.1f}"/>'
        return f'<g fill="{c}" opacity="0.10">{pts}</g>'
    if m == 'ridges':
        out = ''.join(f'<ellipse cx="540" cy="1100" rx="{60 + k * 46 + (t * 6) % 46:.1f}" ry="{90 + k * 70 + (t * 9) % 70:.1f}"/>' for k in range(24))
        return f'<g fill="none" stroke="{c}" stroke-width="2.5" opacity="0.08">{out}</g>'
    if m == 'plus':
        out = ''
        for iy in range(9):
            for ix in range(6):
                x = 100 + ix * 190 + (95 if iy % 2 else 0)
                y = 160 + iy * 210
                out += f'<path d="M{x - 14} {y} H{x + 14} M{x} {y - 14} V{y + 14}"/>'
        return f'<g stroke="{c}" stroke-width="3" stroke-linecap="round" opacity="0.14">{out}</g>'
    if m == 'arcs':
        out = ''.join(f'<circle cx="0" cy="0" r="{r}"/><circle cx="{VW}" cy="{VH}" r="{r}"/>' for r in range(300, 1300, 150))
        return f'<g fill="none" stroke="{c}" stroke-width="3" opacity="0.09">{out}</g>'
    if m == 'blocks':
        dr = 14 * math.sin(t * 0.5)
        return (f'<g fill="{c}" opacity="0.06"><g transform="rotate(-9 540 960)">'
                f'<rect x="-100" y="{300 + dr:.1f}" width="900" height="260" rx="40"/>'
                f'<rect x="300" y="{900 - dr:.1f}" width="900" height="260" rx="40"/>'
                f'<rect x="-100" y="{1500 + dr:.1f}" width="900" height="260" rx="40"/></g></g>')
    if m == 'steps':
        out = ''.join(f'<rect x="0" y="{VH - 160 - k * 130}" width="{180 + k * 150}" height="130"/>' for k in range(8))
        return f'<g fill="{c}" opacity="0.06">{out}</g>'
    return ''


def frame_extra(f):
    c = TH['ln']
    if f == 'brackets':
        return (f'<path d="M92 560 H60 V1360 H92 M988 560 H1020 V1360 H988" fill="none" stroke="{c}" stroke-width="5" '
                f'stroke-linecap="round" stroke-linejoin="round" opacity="0.85"/>')
    if f == 'dashed':
        return (f'<rect x="40" y="40" width="1000" height="1840" rx="30" fill="none" stroke="{c}" stroke-width="3" '
                f'stroke-dasharray="14 12" opacity="0.7"/>')
    if f == 'diamond':
        out = ''.join(f'<rect x="{x - 13}" y="{y - 13}" width="26" height="26" transform="rotate(45 {x} {y})"/>'
                      for x, y in ((60, 60), (1020, 60), (60, 1860), (1020, 1860)))
        return (f'<g fill="{c}" opacity="0.9">{out}</g>'
                f'<rect x="60" y="60" width="960" height="1800" rx="6" fill="none" stroke="{c}" stroke-width="1.5" opacity="0.5"/>')
    if f == 'inset':
        return (f'<rect x="56" y="56" width="968" height="1808" rx="46" fill="{TH["ac"]}" opacity="0.05"/>'
                f'<rect x="56" y="56" width="968" height="1808" rx="46" fill="none" stroke="{c}" stroke-width="2" opacity="0.4"/>')
    if f == 'tab':
        d, w = tp('busma', 30, 'middle', LA500, 0.3)
        return (f'<rect x="40" y="52" width="1000" height="1828" rx="30" fill="none" stroke="{c}" stroke-width="2" opacity="0.75"/>'
                f'<rect x="{540 - w / 2 - 36:.1f}" y="30" width="{w + 72:.1f}" height="46" rx="23" fill="{TH["bg"]}" stroke="{c}" '
                f'stroke-width="2" opacity="1"/><path d="{d}" transform="translate(540 64)" fill="{c}" opacity="0.9"/>')
    if f == 'underline':
        return (f'<line x1="340" y1="228" x2="740" y2="228" stroke="{c}" stroke-width="4" stroke-linecap="round" opacity="0.8"/>'
                f'<line x1="340" y1="1834" x2="740" y2="1834" stroke="{c}" stroke-width="4" stroke-linecap="round" opacity="0.8"/>')
    return ''


def decor_layer(t):
    m = STYLE.get('decor', 'none')
    if m == 'none':
        return ''
    c = TH['ac']
    total = max(CFG.get('total', 1.0), 0.1)
    if m == 'progress':
        return (f'<line x1="110" y1="1802" x2="970" y2="1802" stroke="{c}" stroke-width="6" stroke-linecap="round" opacity="0.2"/>'
                f'<line x1="110" y1="1802" x2="{110 + 860 * clamp(t / total):.1f}" y2="1802" stroke="{c}" stroke-width="6" '
                f'stroke-linecap="round" opacity="0.9"/>')
    if m == 'counter':
        scenes = CFG['scenes']
        idx = 1
        for i, s in enumerate(scenes):
            if t >= s['start']:
                idx = i + 1
        txt = f'{numeral(idx).zfill(1)} / {numeral(len(scenes))}'
        d, _ = tp(txt, 40, 'start')
        return f'<path d="{d}" transform="translate(110 1800)" fill="{c}" opacity="0.8"/>'
    if m == 'corners':
        pts = ''.join(f'<circle cx="{x + k * 26 * (1 if x < 540 else -1)}" cy="{y}" r="6"/>'
                      for x, y in ((90, 110), (990, 110), (90, 1810), (990, 1810)) for k in range(3))
        return f'<g fill="{c}" opacity="0.7">{pts}</g>'
    if m == 'orbit':
        ang = t * 1.4
        out = f'<circle cx="540" cy="140" r="86" fill="none" stroke="{c}" stroke-width="2" opacity="0.35"/>'
        out += f'<circle cx="{540 + 86 * math.cos(ang):.1f}" cy="{140 + 86 * math.sin(ang):.1f}" r="7" fill="{c}" opacity="0.9"/>'
        out += f'<circle cx="540" cy="140" r="118" fill="none" stroke="{c}" stroke-width="1.5" opacity="0.2"/>'
        out += f'<circle cx="{540 + 118 * math.cos(-ang * 0.8 + 2):.1f}" cy="{140 + 118 * math.sin(-ang * 0.8 + 2):.1f}" r="5" fill="{c}" opacity="0.7"/>'
        return out
    if m == 'ticks':
        out = ''
        hi = int((t * 3) % 40)
        for k in range(40):
            x = 110 + k * 22
            h = 26 if k == hi else 14
            out += f'<line x1="{x}" y1="{1810 - h}" x2="{x}" y2="{1810}" opacity="{0.95 if k == hi else 0.4}"/>'
        return f'<g stroke="{c}" stroke-width="3" stroke-linecap="round">{out}</g>'
    if m == 'edgedots':
        out = ''.join(f'<circle cx="{x}" cy="{y}" r="4"/>' for x in (62, 1018) for y in range(320, 1620, 90))
        return f'<g fill="{c}" opacity="0.6">{out}</g>'
    if m == 'crosses':
        out = ''.join(f'<path d="M{x - 16} {y} H{x + 16} M{x} {y - 16} V{y + 16}"/>' for x, y in ((96, 110), (984, 110), (96, 1810), (984, 1810)))
        return f'<g stroke="{c}" stroke-width="4" stroke-linecap="round" opacity="0.8">{out}</g>'
    if m == 'bar':
        return f'<line x1="22" y1="300" x2="22" y2="1620" stroke="{c}" stroke-width="8" stroke-linecap="round" opacity="0.55"/>'
    if m == 'pulse':
        p = (t * 0.5) % 1.0
        return f'<circle cx="540" cy="140" r="{70 + 150 * p:.1f}" fill="none" stroke="{c}" stroke-width="3" opacity="{(1 - p) * 0.5:.3f}"/>'
    if m == 'tag':
        d, w = tp('busma', 30, 'start', LA500, 0.3)
        return f'<path d="{d}" transform="translate(110 1812)" fill="{c}" opacity="0.75"/>'
    return ''



# ================================================================ extended design library (v3): 78 scene designs
VARIANTS['hook'] += ['underline', 'panel']
VARIANTS['contrast'] += ['diagonal', 'swap']
VARIANTS['steps'] += ['chevrons', 'arrows', 'ticks']
VARIANTS['checklist'] += ['chevrons', 'arrows', 'ticks']
VARIANTS['lens'] += ['scan']
VARIANTS['stamp'] += ['frame']
VARIANTS['question'] += ['chat', 'panel']
VARIANTS['bignumber'] += ['stack']
VARIANTS['versus'] += ['halves']
VARIANTS['quote'] += ['brackets', 'highlight']
VARIANTS['outro'] += ['band']


# ---------------------------------------------------------------- extra list styles (steps + checklist)
def list_chevrons(lt, D, items, ytop):
    o, n = '', len(items)
    x1, x2 = 990, 130
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.5)
        if pr <= 0:
            continue
        y = ytop + i * 150
        dx = (1 - pr) * 240
        d = (f'M {x2 + 44} {y - 60} H {x1} L {x1 - 44} {y} L {x1} {y + 60} H {x2 + 44} L {x2} {y} Z')
        o += (f'<g transform="translate({dx:.1f} 0)" opacity="{pr:.3f}"><path d="{d}" fill="{TH["ac"]}" '
              f'fill-opacity="{0.95 - 0.12 * (i % 2):.2f}"/>'
              + T(label, fit_size(label, 66, 640), TH['bg'], x1 - 90, y + 22, 1, 0, 'end') + '</g>')
    return o


def list_arrows(lt, D, items, ytop):
    o, n = '', len(items)
    gap = 190 if n <= 4 else 160
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.45)
        if pr <= 0:
            continue
        y = ytop + i * gap
        k = 0.88 + 0.12 * pr
        o += (f'<g transform="translate(540 {y}) scale({k:.3f}) translate(-540 {-y})" opacity="{pr:.3f}">'
              + rrect(170, y - 56, 740, 112, 30, stroke=TH['ac'], sw=4)
              + T(label, fit_size(label, 64, 620), TH['fg'], 540, y + 22) + '</g>')
        if i < n - 1:
            ar = ease((lt - st - 0.12 * D) / (0.1 * D))
            ay = y + 56 + (gap - 112) / 2
            o += (f'<polyline points="510,{ay - 12} 540,{ay + 14} 570,{ay - 12}" fill="none" stroke="{TH["ac"]}" '
                  f'stroke-width="8" stroke-linecap="round" stroke-linejoin="round" opacity="{ar:.3f}"/>')
    return o


def list_ticks(lt, D, items, ytop):
    o, n = '', len(items)
    for i, label in enumerate(items):
        st = (0.10 + 0.62 * i / max(n - 1, 1)) * D
        pr = ease((lt - st) / 0.5)
        if pr <= 0:
            continue
        y = ytop + i * 160
        o += circ(880, y, 46 * (0.6 + 0.4 * pr), fill=TH['ac'], op=pr)
        cp = ease((lt - st - 0.12) / 0.35)
        if cp > 0:
            L = 70.0
            o += (f'<polyline points="858,{y + 2} 872,{y + 18} 902,{y - 18}" fill="none" stroke="{TH["bg"]}" stroke-width="9" '
                  f'stroke-linecap="round" stroke-linejoin="round" stroke-dasharray="{L} {L + 5}" '
                  f'stroke-dashoffset="{L * (1 - cp):.2f}"/>')
        op, dy = fin(lt, st + 0.05)
        o += T(label, fit_size(label, 70, 660), TH['fg'], 800, y + 24, op, dy, 'end')
        o += hline(880, 880 - 760 * pr, y + 68, TH['fg'], 2, 0.2)
    return o


EXTRA_LISTS.update({'chevrons': list_chevrons, 'arrows': list_arrows, 'ticks': list_ticks})


# ---------------------------------------------------------------- hook
@reg('hook', 'underline')
def sc_hook_underline(lt, D, s):
    o = fp_group(ease((lt - 0.04 * D) / (0.5 * D)), 5.2, 540, 560, TH['ac'], 1.0)
    lines = _lines_of(s, 104, 860, 3)
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.14 + 0.16 * i) * D)
        sz = fit_size(ln, 104, 860)
        y = 860 + i * 170
        o += T(ln, sz, TH['fg'] if i % 2 == 0 else TH['ac'], 540, y, op, dy)
        _, w = tp(ln, sz)
        g = ease((lt - (0.26 + 0.16 * i) * D) / (0.16 * D))
        o += hline(540 + w / 2, 540 + w / 2 - w * g, y + 36, TH['ac'], 8, g)
    return o


@reg('hook', 'panel')
def sc_hook_panel(lt, D, s):
    o = fp_group(ease((lt - 0.04 * D) / (0.6 * D)), 10, 540, 640, TH['ac'], 1.0)
    p = ease((lt - 0.14 * D) / (0.2 * D))
    o += f'<rect x="0" y="{1800 - 800 * p:.1f}" width="1080" height="{800 * p:.1f}" fill="{TH["ac"]}"/>'
    lines = _lines_of(s, 100, 860, 3)
    n = len(lines)
    y0 = 1400 - (n - 1) * 70
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.30 + 0.14 * i) * D)
        o += T(ln, fit_size(ln, 100, 860), TH['bg'], 540, y0 + i * 140, op, dy)
    return o


# ---------------------------------------------------------------- contrast
@reg('contrast', 'diagonal')
def sc_contrast_diagonal(lt, D, s):
    o = ''
    before, after = s['before'], s['after']
    a = ease((lt - 0.05 * D) / (0.16 * D))
    o += (f'<g opacity="{a:.3f}"><polygon points="70,560 1010,470 1010,900 70,990" fill="{TH["fg"]}" opacity="0.08"/>'
          f'<g transform="rotate(-5 540 760)">{T(before, fit_size(before, 108, 800), TH["fg"], 540, 790, 0.7)}</g></g>')
    g = ease((lt - 0.3 * D) / (0.12 * D))
    _, w = tp(before, fit_size(before, 108, 800))
    o += hline(540 + w / 2, 540 + w / 2 - w * g, 755, TH['ac'], 8, g)
    b = ease((lt - 0.5 * D) / (0.2 * D))
    dy = (1 - b) * 120
    o += (f'<g opacity="{b:.3f}" transform="translate(0 {dy:.1f})"><polygon points="70,1060 1010,970 1010,1430 70,1520" '
          f'fill="{TH["ac"]}"/><g transform="rotate(-5 540 1250)">{T(after, fit_size(after, 120, 800), TH["bg"], 540, 1280)}</g></g>')
    return o


@reg('contrast', 'swap')
def sc_contrast_swap(lt, D, s):
    o = ''
    before, after = s['before'], s['after']
    p = eio((lt - 0.40 * D) / (0.30 * D))
    ap = ease((lt - 0.04 * D) / (0.14 * D))
    yb = 760 + 520 * p
    ya = 1280 - 520 * p
    o += T(before, fit_size(before, 120, 860), TH['fg'], 540, yb, ap * (1 - 0.55 * p))
    o += T(after, fit_size(after, 120, 860), TH['ac'], 540, ya, ap * (0.45 + 0.55 * p))
    o += hline(240, 840, 1020, TH['fg'], 3, 0.25 * ap)
    ar = ease((lt - 0.34 * D) / (0.1 * D)) * (1 - p)
    o += (f'<polyline points="500,{1000} 540,{1040} 580,{1000}" fill="none" stroke="{TH["ac"]}" stroke-width="9" '
          f'stroke-linecap="round" stroke-linejoin="round" opacity="{ar:.3f}"/>')
    return o


# ---------------------------------------------------------------- lens
@reg('lens', 'scan')
def sc_lens_scan(lt, D, s):
    o = ''
    l1, l2 = s['line1'], s['line2']
    s1, s2 = fit_size(l1, 124), fit_size(l2, 130)
    ap = ease((lt - 0.04 * D) / (0.14 * D))
    o += T(l1, s1, TH['fg'], 540, 780, ap * 0.55) + T(l2, s2, TH['fg'], 540, 960, ap * 0.55)
    q = eio((lt - 0.20 * D) / (0.55 * D))
    ys = 560 + 560 * q
    if q > 0:
        o += (f'<clipPath id="sc"><rect x="0" y="0" width="1080" height="{ys:.1f}"/></clipPath><g clip-path="url(#sc)">'
              f'{T(l1, s1, TH["fg"], 540, 780)}{T(l2, s2, TH["ac"], 540, 960)}</g>')
        la = clamp((0.95 * D - lt) / 0.3) * clamp((lt - 0.2 * D) / 0.2)
        o += (f'<g opacity="{la:.3f}"><line x1="100" y1="{ys:.1f}" x2="980" y2="{ys:.1f}" stroke="{TH["ac"]}" '
              f'stroke-width="6" stroke-linecap="round"/><line x1="100" y1="{ys - 14:.1f}" x2="980" y2="{ys - 14:.1f}" '
              f'stroke="{TH["ac"]}" stroke-width="2" opacity="0.5"/></g>')
    return o


# ---------------------------------------------------------------- stamp
@reg('stamp', 'frame')
def sc_stamp_frame(lt, D, s):
    o = ''
    x, y, w, h = 110, 600, 860, 700
    per = 2 * (w + h)
    pr = ease((lt - 0.05 * D) / (0.4 * D))
    o += (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="30" fill="none" stroke="{TH["ac"]}" stroke-width="7" '
          f'stroke-dasharray="{per * pr:.1f} {per:.1f}"/>')
    o += circ(540, y, 100, fill=TH['bg'], op=pr)
    o += fp_group(ease((lt - 0.2 * D) / (0.4 * D)), 4.6, 540, y, TH['ac'], 1.1)
    lines = wrap_lines(s['text'], 108, 760)[:3]
    n = len(lines)
    y0 = y + h / 2 - (n - 1) * 62 + 40
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.32 + 0.12 * i) * D)
        o += T(ln, fit_size(ln, 108, 760), TH['fg'], 540, y0 + i * 124, op, dy)
    return o


# ---------------------------------------------------------------- question
@reg('question', 'chat')
def sc_question_chat(lt, D, s):
    o = ''
    lines = _lines_of(s, 76, 640, 3)
    typing = clamp((0.26 * D - lt) / 0.2) * ease((lt - 0.02 * D) / 0.2)
    if typing > 0:
        o += rrect(700, 600, 240, 110, 55, fill=TH['ac'], op=typing * 0.25)
        for k in range(3):
            by = 655 - 14 * max(0.0, math.sin(lt * 9 - k * 0.9))
            o += circ(760 + k * 60, by, 11, fill=TH['ac'], op=typing)
    q = ease((lt - 0.28 * D) / (0.14 * D))
    n = len(lines)
    h = n * 100 + 90
    if q > 0:
        k = 0.9 + 0.1 * q
        o += (f'<g transform="translate(940 600) scale({k:.3f}) translate(-940 -600)" opacity="{q:.3f}">'
              + rrect(140, 560, 800, h, 50, fill=TH['ac'], op=0.18) + rrect(140, 560, 800, h, 50, stroke=TH['ac'], sw=4) + '</g>')
        for i, ln in enumerate(lines):
            o += T(ln, fit_size(ln, 76, 640), TH['fg'], 540, 560 + 92 + i * 100, q)
    if s.get('answer'):
        a = ease((lt - 0.56 * D) / (0.16 * D))
        ya = 560 + h + 70
        if a > 0:
            k = 0.9 + 0.1 * a
            o += (f'<g transform="translate(140 {ya + 80}) scale({k:.3f}) translate(-140 {-(ya + 80)})" opacity="{a:.3f}">'
                  + rrect(140, ya, 660, 160, 50, fill=TH['ac']) + T(s['answer'], fit_size(s['answer'], 76, 560), TH['bg'], 470, ya + 100)
                  + '</g>')
    return o


@reg('question', 'panel')
def sc_question_panel(lt, D, s):
    o = ''
    lines = _lines_of(s, 96, 800, 3)
    n = len(lines)
    y0 = 760 - (n - 1) * 56
    a = ease((lt - 0.02 * D) / (0.14 * D))
    o += T('؟', 220, TH['ac'], 540, 520, a * 0.9)
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.12 + 0.1 * i) * D)
        o += T(ln, fit_size(ln, 96, 800), TH['fg'], 540, y0 + 60 + i * 112, op, dy)
    g = ease((lt - 0.42 * D) / (0.14 * D))
    o += hline(120, 120 + 840 * g, 1120, TH['ac'], 4, 0.8)
    o += circ(540, 1120, 20 * g, fill=TH['ac'])
    if s.get('answer'):
        op, dy = fin(lt, 0.56 * D)
        ans = s['answer']
        sz = fit_size(ans, 104, 820)
        o += T(ans, sz, TH['ac'], 540, 1330, op, dy)
    return o


# ---------------------------------------------------------------- bignumber
@reg('bignumber', 'stack')
def sc_bignumber_stack(lt, D, s):
    o = ''
    val = max(1, min(int(s['value']), 10))
    if s.get('label'):
        op, dy = fin(lt, 0.02 * D)
        o += T(s['label'], fit_size(s['label'], 76), TH['fg'], 540, 540, op, dy)
    op = ease((lt - 0.04 * D) / (0.12 * D))
    o += T(str(val).translate(AR_DIGITS), 260, TH['ac'], 540, 800, op)
    h, gap, base = 62, 12, 1560
    for i in range(val):
        t0 = (0.16 + 0.5 * i / max(val - 1, 1)) * D
        pr = ease((lt - t0) / 0.4)
        if pr <= 0:
            continue
        y = base - i * (h + gap) - (1 - pr) * 120
        o += rrect(300, y - h, 480, h, 18, fill=TH['ac'], op=pr * (0.55 + 0.45 * (i + 1) / val))
    if s.get('caption'):
        op, dy = fin(lt, 0.72 * D)
        o += T(s['caption'], fit_size(s['caption'], 66, 860), TH['fg'], 540, 1680, op, dy)
    return o


# ---------------------------------------------------------------- versus
@reg('versus', 'halves')
def sc_versus_halves(lt, D, s):
    o = ''
    a, b = s['a'], s['b']
    pa, pb = a['points'][0], b['points'][0]
    p1 = ease((lt - 0.05 * D) / (0.16 * D))
    o += f'<rect x="{1080 - 520 * p1:.1f}" y="520" width="{520 * p1:.1f}" height="1000" fill="{TH["fg"]}" opacity="0.08"/>'
    o += T(a['title'], 54, TH['fg'], 800, 620, p1 * 0.75)
    for i, ln in enumerate(wrap_lines(pa, 54, 400)[:5]):
        op, dy = fin(lt, (0.12 + 0.05 * i) * D)
        y = 800 + i * 88
        sz = fit_size(ln, 54, 400)
        o += T(ln, sz, TH['fg'], 800, y, op * 0.6, dy)
        _, w = tp(ln, sz)
        g = ease((lt - (0.34 + 0.05 * i) * D) / (0.12 * D))
        o += hline(800 + w / 2, 800 + w / 2 - w * g, y - 20, TH['ac'], 6, g)
    p2 = ease((lt - 0.50 * D) / (0.18 * D))
    o += f'<rect x="60" y="520" width="{520 * p2:.1f}" height="1000" fill="{TH["ac"]}"/>'
    op, dy = fin(lt, 0.56 * D)
    o += T(b['title'], 54, TH['bg'], 320, 620, op * 0.9, dy)
    for i, ln in enumerate(wrap_lines(pb, 58, 400)[:5]):
        op, dy = fin(lt, (0.62 + 0.05 * i) * D)
        o += T(ln, fit_size(ln, 58, 400), TH['bg'], 320, 800 + i * 92, op, dy)
    return o


# ---------------------------------------------------------------- quote
@reg('quote', 'brackets')
def sc_quote_brackets(lt, D, s):
    o = ''
    lines = wrap_lines(s['text'], 90, 780)[:5]
    n = len(lines)
    h = n * 130 + 120
    y0 = 950 - h / 2
    p = ease((lt - 0.03 * D) / (0.3 * D))
    L = 150 * p
    o += (f'<path d="M 130 {y0 + L:.1f} V {y0:.1f} H {130 + L:.1f}" fill="none" stroke="{TH["ac"]}" stroke-width="9" '
          f'stroke-linecap="round" stroke-linejoin="round"/>'
          f'<path d="M 950 {y0 + h - L:.1f} V {y0 + h:.1f} H {950 - L:.1f}" fill="none" stroke="{TH["ac"]}" stroke-width="9" '
          f'stroke-linecap="round" stroke-linejoin="round"/>')
    for i, ln in enumerate(lines):
        op, dy = fin(lt, (0.14 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, fit_size(ln, 90, 780), TH['fg'], 540, y0 + 110 + i * 130, op, dy)
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.72 * D)
        o += pill_text(by, 540, y0 + h + 120, fit_size(by, 52, 560), True, op, dy)
    return o


@reg('quote', 'highlight')
def sc_quote_highlight(lt, D, s):
    o = ''
    lines = wrap_lines(s['text'], 90, 820)[:5]
    n = len(lines)
    y0 = 800 - (n - 1) * 8
    for i, ln in enumerate(lines):
        sz = fit_size(ln, 90, 820)
        _, w = tp(ln, sz)
        y = y0 + i * 140
        g = ease((lt - (0.10 + 0.5 * i / max(n, 1)) * D) / (0.16 * D))
        xr = 540 + w / 2 + 24
        o += rrect(xr - (w + 48) * g, y - 84, (w + 48) * g, 120, 16, fill=TH['ac'], op=0.28)
        op, dy = fin(lt, (0.12 + 0.5 * i / max(n, 1)) * D)
        o += T(ln, sz, TH['fg'], 540, y, op, dy)
    by = s.get('by')
    if by:
        op, dy = fin(lt, 0.74 * D)
        o += pill_text(by, 540, y0 + n * 140 + 110, fit_size(by, 52, 560), False, op, dy)
    return o


# ---------------------------------------------------------------- outro
@reg('outro', 'band')
def sc_outro_band(lt, D, s):
    name, slogan, cta = s.get('name', 'بصمة'), s.get('slogan', 'بصمتك على بحثك'), s.get('cta', 'راسلنا هسه')
    latin = s.get('latin', 'busma')
    o = ''
    b = ease((lt - 0.15) / 0.7)
    sc = 3.4 * (0.92 + 0.08 * b)
    o += (f'<g opacity="{b:.3f}" transform="translate({540 - 50 * sc:.1f} {560 - 50 * sc:.1f}) scale({sc:.3f})">'
          f'{mark_group(TH["wm"])}</g>')
    p = ease((lt - 0.5) / 0.5)
    o += f'<rect x="{1080 - 1080 * p:.1f}" y="860" width="{1080 * p:.1f}" height="300" fill="{TH["ac"]}"/>'
    op, dy = fin(lt, 0.9)
    o += T(name, 220, TH['bg'], 540, 1040, op, dy)
    if latin:
        op, dy = fin(lt, 1.1)
        o += T(latin, 46, TH['bg'], 540, 1112, op * 0.85, dy, font=LA500, tracking=0.34)
    op, dy = fin(lt, 1.3)
    o += T(slogan, 62, TH['fg'], 540, 1290, op * 0.9, dy, font=AR400)
    op, dy = fin(lt, 1.7)
    if op > 0:
        o += rrect(300, 1430 + dy, 480, 116, 58, stroke=TH['ac'], sw=4, op=op)
        o += T(cta, fit_size(cta, 62, 400), TH['fg'], 540, 1507, op, dy)
    return o


def allowed_variants(s):
    t = s['type']
    v = list(VARIANTS.get(t, []))
    if t == 'versus':
        v = ['stack', 'flip', 'tabs', 'bands', 'halves'] if _single(s) else ['columns', 'cardcols']
    if t == 'bignumber':
        try:
            n = int(s['value'])
            if not 1 <= n <= 12:
                v = ['count']
            elif n > 10:
                v = [x for x in v if x not in ('bars', 'stack')]
        except Exception:
            v = ['count']
    return v



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
    st = dict(theme='navy', frame='thin', transition='fade', watermark='top', bg='solid', reveal='rise', decor='none')
    if 'seed' in cfg:
        r = random.Random(cfg['seed'])
        st.update(theme=r.choice(list(THEMES)),
                  frame=r.choice(FRAMES), transition=r.choice(TRANSITIONS),
                  bg=r.choice(BACKGROUNDS), reveal=r.choice(REVEALS), decor=r.choice(DECORS))
    st.update(cfg.get('style', {}))
    return st


def auto_variants(cfg, scenes):
    """If the script has a seed (or auto_variants), pick a variant for every scene without one."""
    if 'seed' not in cfg and not cfg.get('auto_variants'):
        return
    seed = int(cfg.get('seed', 0))
    used = {}
    for i, s in enumerate(scenes):
        if s.get('variant'):
            continue
        opts = allowed_variants(s)
        if not opts:
            continue
        r = random.Random(seed * 131 + i * 7919)
        pool = [o for o in opts if o not in used.get(s['type'], [])] or opts
        s['variant'] = r.choice(pool)
        used.setdefault(s['type'], []).append(s['variant'])


CALM_BGS = ['solid', 'dots', 'grid', 'diag', 'rings', 'waves', 'halftone', 'plus', 'arcs']
MID_BGS = CALM_BGS + ['stripes', 'ridges', 'orbs', 'blocks', 'steps', 'bokeh']
FAMILIES = [['navy', 'night', 'slate', 'ink'], ['champagne', 'ivory', 'cream', 'sand', 'mist', 'gold']]


def auto_looks(cfg, scenes):
    """Variety inside one reel: most middle scenes get a different background, a theme from the same
    colour family, or a different text reveal. Hook and outro keep the base look; frame, transition
    and decor stay fixed so the reel still feels like one piece."""
    if 'seed' not in cfg and not cfg.get('auto_looks'):
        return
    n = len(scenes)
    if n < 3:
        return
    r = random.Random(int(cfg.get('seed', 0)) * 977 + 5)
    base_theme, base_bg, base_rv = STYLE.get('theme', 'navy'), STYLE.get('bg', 'solid'), STYLE.get('reveal', 'rise')
    fam = next((f for f in FAMILIES if base_theme in f), FAMILIES[0])
    prev = None
    for i in range(1, n - 1):
        if scenes[i].get('look'):
            prev = scenes[i]['look']
            continue
        if i != 1 and r.random() > 0.72:
            prev = None
            continue
        kind = r.choices(['bg', 'theme', 'both', 'reveal'], weights=[34, 22, 24, 20])[0]
        look = {}
        if kind in ('bg', 'both', 'reveal'):
            look['bg'] = r.choice([b for b in MID_BGS if b != base_bg and not (prev and b == prev.get('bg'))])
        if kind in ('theme', 'both'):
            look['theme'] = r.choice([t for t in fam if t != base_theme and not (prev and t == prev.get('theme'))])
        if kind == 'reveal':
            look['reveal'] = r.choice([x for x in REVEALS if x != base_rv])
        scenes[i]['look'] = look
        prev = look


def bg_layer(t):
    m = STYLE.get('bg', 'solid')
    c = TH['ac']
    if m == 'dots':
        sp = 64
        off = (t * 7) % sp
        pts = ''.join(f'<circle cx="{36 + ix * sp}" cy="{64 + iy * sp - off:.1f}" r="2.6"/>'
                      for ix in range(17) for iy in range(31) if 58 < 64 + iy * sp - off < 1862 and 36 + ix * sp < 1050)
        return f'<g fill="{c}" opacity="0.12">{pts}</g>'
    if m == 'grid':
        sp = 120
        off = (t * 5) % sp
        ln = ''.join(f'<line x1="{x}" y1="0" x2="{x}" y2="{VH}"/>' for x in range(sp, VW, sp))
        ln += ''.join(f'<line x1="0" y1="{y - off:.1f}" x2="{VW}" y2="{y - off:.1f}"/>' for y in range(sp, VH + sp, sp))
        return f'<g stroke="{c}" stroke-width="1.5" opacity="0.08">{ln}</g>'
    if m == 'diag':
        off = (t * 9) % 80
        ln = ''.join(f'<line x1="{k * 80 + off:.1f}" y1="0" x2="{k * 80 + off + 960:.1f}" y2="{VH}"/>' for k in range(-14, 16))
        return f'<g stroke="{c}" stroke-width="2" opacity="0.07">{ln}</g>'
    if m == 'rings':
        grow = (t * 14) % 170
        cs = ''.join(f'<circle cx="540" cy="960" r="{r + grow:.1f}"/>' for r in range(40, 1500, 170))
        return f'<g fill="none" stroke="{c}" stroke-width="2.5" opacity="0.09">{cs}</g>'
    if m == 'waves':
        out = ''
        for i in range(11):
            y0 = 120 + i * 168
            pts = ' L '.join(f'{x} {y0 + 30 * math.sin(x / 120 + t * 0.9 + i * 0.7):.1f}' for x in range(0, VW + 60, 60))
            out += f'<path d="M {pts}"/>'
        return f'<g fill="none" stroke="{c}" stroke-width="2.5" opacity="0.10">{out}</g>'
    if m == 'orbs':
        dr = 18 * math.sin(t * 0.6)
        return (f'<g fill="{c}"><circle cx="-40" cy="{220 + dr:.1f}" r="520" opacity="0.07"/>'
                f'<circle cx="1130" cy="{1740 - dr:.1f}" r="600" opacity="0.07"/>'
                f'<circle cx="930" cy="{440 + dr:.1f}" r="170" opacity="0.05"/></g>')
    if m == 'print':
        return fp_group(1.0, 40, 860, 1380, c, 0.55, 0.08)
    return bg_extra(m, t)


def frame_deco():
    c, f = TH['ln'], STYLE['frame']
    if f == 'none':
        return ''
    if f in ('brackets', 'dashed', 'diamond', 'inset', 'tab', 'underline'):
        return frame_extra(f)
    if f == 'thin':
        return (f'<rect x="40" y="40" width="1000" height="1840" rx="30" fill="none" stroke="{c}" '
                f'stroke-width="2" opacity="0.8"/>')
    if f == 'double':
        return (f'<rect x="40" y="40" width="1000" height="1840" rx="30" fill="none" stroke="{c}" '
                f'stroke-width="3" opacity="0.85"/>'
                f'<rect x="60" y="60" width="960" height="1800" rx="22" fill="none" stroke="{c}" '
                f'stroke-width="1.5" opacity="0.5"/>')
    if f == 'bars':
        return (f'<line x1="90" y1="228" x2="990" y2="228" stroke="{c}" stroke-width="3" opacity="0.8"/>'
                f'<line x1="90" y1="1834" x2="990" y2="1834" stroke="{c}" stroke-width="3" opacity="0.8"/>')
    if f == 'rails':
        ticks = ''.join(f'<line x1="{x}" y1="{y}" x2="{x + (16 if x < 500 else -16)}" y2="{y}"/>'
                        for x in (66, 1014) for y in range(300, 1640, 110))
        return (f'<g stroke="{c}" stroke-width="3" opacity="0.75"><line x1="66" y1="300" x2="66" y2="1620"/>'
                f'<line x1="1014" y1="300" x2="1014" y2="1620"/>{ticks}</g>')
    d = ('M52 170 V52 H170 M1028 170 V52 H910 M52 1750 V1868 H170 M1028 1750 V1868 H910')
    return (f'<path d="{d}" fill="none" stroke="{c}" stroke-width="5" stroke-linecap="round" '
            f'stroke-linejoin="round" opacity="0.9"/>')


def frame_svg(t):
    scenes = CFG['scenes']
    body = f'<rect width="{VW}" height="{VH}" fill="{TH["bg"]}"/>' + bg_layer(t) + frame_deco() + decor_layer(t)
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
        tf, pre, post = '', '', ''
        if tr == 'slide':
            tf = f' transform="translate(0 {(1 - oi) * 70 - (1 - oo) * 70:.1f})"'
        elif tr == 'slidedown':
            tf = f' transform="translate(0 {-(1 - oi) * 70 + (1 - oo) * 70:.1f})"'
        elif tr == 'slidex':
            tf = f' transform="translate({(1 - oi) * 110 - (1 - oo) * 110:.1f} 0)"'
        elif tr == 'rise':
            tf = f' transform="translate(0 {(1 - oi) * 28 - (1 - oo) * 28:.1f})"'
        elif tr == 'zoom':
            k = 0.94 + 0.06 * oi + (1 - oo) * 0.06
            tf = f' transform="translate(540 960) scale({k:.4f}) translate(-540 -960)"'
        elif tr == 'flip':
            sx = max(0.02, oi * oo)
            tf = f' transform="translate(540 960) scale({sx:.4f} 1) translate(-540 -960)"'
        elif tr == 'tilt':
            ang = -(1 - oi) * 3.5 + (1 - oo) * 3.5
            tf = f' transform="rotate({ang:.2f} 540 960)"'
        elif tr == 'wipe' and oi < 1:
            pw = VW * ease(oi)
            pre = (f'<clipPath id="wp{i}"><rect x="{VW - pw:.1f}" y="0" width="{pw:.1f}" height="{VH}"/></clipPath>'
                   f'<g clip-path="url(#wp{i})">')
            post = '</g>'
            o = 1.0 if oo >= 1 else o
        elif tr == 'iris' and oi < 1:
            pre = (f'<clipPath id="ir{i}"><circle cx="540" cy="960" r="{1300 * ease(oi):.1f}"/></clipPath>'
                   f'<g clip-path="url(#ir{i})">')
            post = '</g>'
            o = 1.0 if oo >= 1 else o
        look = s.get('look') or {}
        if look:
            saved_th, saved_bg, saved_rv = dict(TH), STYLE.get('bg'), STYLE.get('reveal')
            if look.get('theme') in THEMES:
                TH.update(THEMES[look['theme']])
            if look.get('bg'):
                STYLE['bg'] = look['bg']
            if look.get('reveal') in REVEALS:
                STYLE['reveal'] = look['reveal']
            inner = (f'<rect width="{VW}" height="{VH}" fill="{TH["bg"]}"/>' + bg_layer(t) + frame_deco() + decor_layer(t)
                     + scene_svg(s, lt))
            TH.clear()
            TH.update(saved_th)
            STYLE['bg'] = saved_bg
            STYLE['reveal'] = saved_rv
        else:
            inner = scene_svg(s, lt)
        body += f'{pre}<g opacity="{o:.3f}"{tf}>{inner}</g>{post}'
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
    auto_variants(cfg, scenes)
    auto_looks(cfg, scenes)
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
