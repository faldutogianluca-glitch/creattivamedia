#!/usr/bin/env python3
"""Reel Check di Creattiva: analisi di un video breve prima di pubblicarlo.

Misura il video (formato, ritmo visivo, audio, parlato), lo confronta con le regole
editoriali di Creattiva e salva le misure in uno storico. Non stima la "viralita":
quando lo storico ha abbastanza post con i loro risultati, il comando `learn`
mostra quali misure vanno davvero insieme a piu visualizzazioni o commenti.

Uso:
  python3 reelcheck.py analyze VIDEO [--srt FILE] [--caption FILE] [--out DIR] [--ollama-model NOME]
  python3 reelcheck.py normalize VIDEO [--output FILE]
  python3 reelcheck.py learn  METRICHE.csv [--metric views] [--history storico.csv]

Requisiti: Python 3.9+, numpy, Pillow, ffmpeg/ffprobe nel PATH.
Modello locale facoltativo: Ollama (http://localhost:11434), per proporre ganci e didascalie.
"""
import argparse, base64, csv, io, json, os, re, subprocess, sys, urllib.request
from datetime import datetime
import numpy as np
from PIL import Image

# Regole editoriali di Creattiva: soglie scelte da noi, non leggi delle piattaforme.
RULES = {
    "durata_min": 15, "durata_max": 60,
    "prima_parola_max": 1.0,
    "primo_cambio_max": 3.0,
    "intervallo_cambi_max": 5.0,
    "silenzio_max": 0.7,
    "lufs_min": -18.0, "lufs_max": -10.0,
    "wpm_min": 130, "wpm_max": 190,
}
CTA_PATTERNS = [r"\bcommentate\b", r"\bcommenta\b", r"\bscrivete\b", r"\bscrivi\b", r"\bcliccate\b",
                r"\biscrivetevi\b", r"\bprenotate\b", r"\blink\b", r"\bin privato\b", r"\bdirect\b"]
AI_NOTE = re.compile(r"ausilio dell.?AI|generat\w+ con l.?AI|avatar AI", re.I)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def probe(path):
    out = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]).stdout)
    v = next(s for s in out["streams"] if s["codec_type"] == "video")
    a = [s for s in out["streams"] if s["codec_type"] == "audio"]
    num, den = (int(x) for x in v.get("avg_frame_rate", "0/1").split("/"))
    return {"durata": float(out["format"]["duration"]), "larghezza": int(v["width"]), "altezza": int(v["height"]),
            "fps": round(num / den, 2) if den else 0, "audio": bool(a)}


def motion(path, fps=10):
    """Differenza media tra fotogrammi consecutivi (0-255), a 10 fotogrammi al secondo."""
    w = 96
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={w}:-2,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    info = probe(path)
    h = int(round(info["altezza"] * w / info["larghezza"] / 2) * 2)
    frames = np.frombuffer(p.stdout, dtype=np.uint8)
    n = frames.size // (w * h)
    frames = frames[: n * w * h].reshape(n, h, w).astype(np.float32)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2)) if n > 1 else np.zeros(1)
    t = (np.arange(len(diff)) + 1) / fps
    thr = max(12.0, 4 * float(np.median(diff)))
    cuts, last = [], -1.0
    for ti, d in zip(t, diff):
        if d > thr and ti - last > 0.5:
            cuts.append(round(float(ti), 2)); last = ti
    return t, diff, cuts


def loudness(path):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "verbose", "-i", path, "-af", "ebur128", "-f", "null", "-"])
    ts, ms = [], []
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)", r.stderr):
        ts.append(float(m.group(1))); ms.append(-70.0 if m.group(2) == "-inf" else float(m.group(2)))
    integ = re.findall(r"I:\s*(-?[\d.]+) LUFS", r.stderr)
    s = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"]).stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", s)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", s)]
    gaps = [(round(a, 2), round(b - a, 2)) for a, b in zip(starts, ends)]
    first = next((t for t, m in zip(ts, ms) if m > -40), None)
    return np.array(ts), np.array(ms), (float(integ[-1]) if integ else None), gaps, first


def read_srt(path):
    def sec(x):
        h, m, s = x.split(":"); s, ms = s.split(","); return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    caps = []
    for b in open(path, encoding="utf-8").read().strip().split("\n\n"):
        l = b.strip().split("\n")
        if len(l) >= 3 and "-->" in l[1]:
            a, z = l[1].split(" --> "); caps.append((sec(a.strip()), sec(z.strip()), " ".join(l[2:])))
    return caps


def frames_strip(path, dur, k=6):
    out = []
    for i in range(k):
        t = min(dur - 0.1, 0.5 + i * (dur - 0.6) / max(1, k - 1))
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", path, "-frames:v", "1", "-vf", "scale=180:-2",
                            "-f", "image2pipe", "-vcodec", "mjpeg", "-"], capture_output=True)
        if p.stdout:
            out.append((round(t, 1), base64.b64encode(p.stdout).decode()))
    return out


def count_ctas(text):
    return sum(len(re.findall(p, text, re.I)) for p in CTA_PATTERNS)


def ollama(model, transcript, caption):
    prompt = ("Sei l'editor social di Creattiva, azienda italiana che installa AI in locale nelle PMI. "
              "Dato il parlato di un video breve, proponi in italiano: 3 ganci alternativi per i primi 3 secondi (massimo 12 parole), "
              "e 2 didascalie con una sola chiamata all'azione e la riga finale 'Contenuto realizzato con l'ausilio dell'AI con la supervisione di Gianluca Falduto.' "
              "Niente numeri o clienti inventati. Rispondi in JSON con chiavi 'ganci' e 'didascalie'.\n\n"
              f"PARLATO:\n{transcript}\n\nDIDASCALIA ATTUALE:\n{caption}")
    req = urllib.request.Request(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/generate",
                                 data=json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(json.loads(r.read())["response"])
    except Exception as e:
        return {"errore": f"Modello locale non raggiungibile ({e.__class__.__name__}). Avviate Ollama per avere ganci e didascalie."}


def analyze(args):
    v = args.video
    info = probe(v)
    t, diff, cuts = motion(v)
    lt, lm, lufs, gaps, first_sound = loudness(v) if info["audio"] else (np.array([]), np.array([]), None, [], None)
    caps = read_srt(args.srt) if args.srt else []
    caption = open(args.caption, encoding="utf-8").read() if args.caption else ""
    dur = info["durata"]
    transcript = " ".join(c[2] for c in caps)
    words = len(transcript.split())
    speech_time = sum(z - a for a, z, _ in caps)
    wpm = round(words / speech_time * 60) if speech_time else None
    first_word = round(caps[0][0], 2) if caps else (round(first_sound, 2) if first_sound is not None else None)
    hook = " ".join(c[2] for c in caps if c[0] < 3.0)
    intervals = np.diff([0.0] + cuts + [dur]) if cuts else np.array([dur])
    inner_gaps = [g for g in gaps if g[0] > 0.5 and g[0] + g[1] < dur - 0.5]
    longest_gap = max((g[1] for g in inner_gaps), default=0.0)
    ctas_spoken, ctas_caption = count_ctas(transcript), count_ctas(caption)
    M = {
        "file": os.path.basename(v), "analizzato": datetime.now().isoformat(timespec="seconds"),
        **info, "verticale_9x16": abs(info["altezza"] / info["larghezza"] - 16 / 9) < 0.02,
        "cambi_visivi": len(cuts), "primo_cambio_s": cuts[0] if cuts else None,
        "intervallo_medio_cambi_s": round(float(intervals.mean()), 2),
        "movimento_0_3s": round(float(diff[t <= 3].mean()), 2) if len(diff) else None,
        "movimento_medio": round(float(diff.mean()), 2) if len(diff) else None,
        "lufs_integrato": lufs, "primo_suono_s": first_sound, "silenzio_piu_lungo_s": longest_gap,
        "prima_parola_s": first_word, "parole": words, "parole_al_minuto": wpm,
        "gancio_primi_3s": hook, "gancio_ha_domanda": "?" in hook, "gancio_ha_numero": bool(re.search(r"\d|cento|mille", hook, re.I)),
        "cta_nel_parlato": ctas_spoken, "cta_in_didascalia": ctas_caption,
        "nota_ai_in_didascalia": bool(AI_NOTE.search(caption)) if caption else None,
        "hashtag": len(re.findall(r"#\w+", caption)),
    }
    R = RULES
    def chk(name, ok, detail):
        return {"controllo": name, "esito": "n.d." if ok is None else ("ok" if ok else "da rivedere"), "dettaglio": detail}
    C = [
        chk("Formato verticale 9:16", M["verticale_9x16"], f"{info['larghezza']}×{info['altezza']}"),
        chk(f"Durata tra {R['durata_min']} e {R['durata_max']} s", R["durata_min"] <= dur <= R["durata_max"], f"{dur:.1f} s"),
        chk(f"Prima parola entro {R['prima_parola_max']:.0f} s", None if first_word is None else first_word <= R["prima_parola_max"], f"{first_word} s" if first_word is not None else "nessun parlato rilevato"),
        chk(f"Un cambio visivo entro {R['primo_cambio_max']:.0f} s", None if not len(diff) else bool(cuts and cuts[0] <= R["primo_cambio_max"]), f"primo cambio a {cuts[0]} s" if cuts else "nessun cambio rilevato"),
        chk(f"Un cambio visivo almeno ogni {R['intervallo_cambi_max']:.0f} s in media", M["intervallo_medio_cambi_s"] <= R["intervallo_cambi_max"], f"{len(cuts)} cambi, uno ogni {M['intervallo_medio_cambi_s']} s"),
        chk(f"Nessun silenzio oltre {R['silenzio_max']} s", None if not info["audio"] else longest_gap <= R["silenzio_max"], f"pausa più lunga {longest_gap} s"),
        chk(f"Volume tra {R['lufs_min']:.0f} e {R['lufs_max']:.0f} LUFS", None if lufs is None else R["lufs_min"] <= lufs <= R["lufs_max"], f"{lufs} LUFS" if lufs is not None else "n.d."),
        chk(f"Ritmo parlato tra {R['wpm_min']} e {R['wpm_max']} parole/min", None if wpm is None else R["wpm_min"] <= wpm <= R["wpm_max"], f"{wpm} parole/min" if wpm else "serve il file dei sottotitoli"),
        chk("Sottotitoli disponibili", bool(caps) if args.srt else None, "file .srt fornito" if caps else "non verificato: fornite il file .srt"),
        chk("Una sola chiamata all'azione", None if not (caption or caps) else max(ctas_spoken, ctas_caption) <= 2 and len(set(re.findall(r"\b[A-Z]{4,}\b", transcript + ' ' + caption)) - {"CREATTIVA"}) <= 1,
            f"parlato {ctas_spoken}, didascalia {ctas_caption} richiami"),
        chk("Nota sull'uso dell'AI in didascalia", M["nota_ai_in_didascalia"], "presente" if M["nota_ai_in_didascalia"] else ("mancante" if caption else "didascalia non fornita")),
    ]
    sugg = None
    if args.ollama_model:
        sugg = ollama(args.ollama_model, transcript, caption)
    os.makedirs(args.out, exist_ok=True)
    base = os.path.join(args.out, os.path.splitext(os.path.basename(v))[0] + "-reelcheck")
    json.dump({"misure": M, "controlli": C, "suggerimenti": sugg}, open(base + ".json", "w"), ensure_ascii=False, indent=2)
    open(base + ".html", "w", encoding="utf-8").write(report_html(M, C, t, diff, cuts, lt, lm, caps, frames_strip(v, dur), sugg))
    hist = args.history
    new = not os.path.exists(hist)
    with open(hist, "a", newline="", encoding="utf-8") as f:
        keys = [k for k in M if k not in ("gancio_primi_3s",)]
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        if new: w.writeheader()
        w.writerow(M)
    ok = sum(c["esito"] == "ok" for c in C); tot = sum(c["esito"] != "n.d." for c in C)
    print(f"Controlli superati: {ok}/{tot}")
    for c in C: print(f"  [{c['esito']:^12}] {c['controllo']}: {c['dettaglio']}")
    print(f"Report: {base}.html\nStorico aggiornato: {hist}")


def spark(xs, ys, w, h, lo, hi, color, marks=()):
    if len(xs) == 0: return ""
    X = lambda x: x / max(xs[-1], 1e-6) * w
    Y = lambda y: h - (min(max(y, lo), hi) - lo) / (hi - lo) * h
    pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))
    m = "".join(f"<line x1='{X(c):.1f}' x2='{X(c):.1f}' y1='0' y2='{h}' stroke='#3a9ed2' stroke-dasharray='3 3'/>" for c in marks)
    return f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' style='width:100%;height:{h}px'>{m}<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.6'/></svg>"


def report_html(M, C, t, diff, cuts, lt, lm, caps, strip, sugg):
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    ok = sum(c["esito"] == "ok" for c in C); tot = sum(c["esito"] != "n.d." for c in C)
    rows = "".join(f"<tr class='{ 'ok' if c['esito']=='ok' else ('nd' if c['esito']=='n.d.' else 'ko')}'><td>{ '✓' if c['esito']=='ok' else ('–' if c['esito']=='n.d.' else '✗')}</td><td>{esc(c['controllo'])}</td><td>{esc(c['dettaglio'])}</td></tr>" for c in C)
    imgs = "".join(f"<figure><img src='data:image/jpeg;base64,{b}'><figcaption>{s} s</figcaption></figure>" for s, b in strip)
    sg = ""
    if sugg:
        if "errore" in sugg: sg = f"<p class='muted'>{esc(sugg['errore'])}</p>"
        else:
            sg = "<h3>Ganci alternativi</h3><ol>" + "".join(f"<li>{esc(x)}</li>" for x in sugg.get("ganci", [])) + "</ol><h3>Didascalie</h3>" + "".join(f"<pre>{esc(x)}</pre>" for x in sugg.get("didascalie", []))
    trans = "".join(f"<p><span class='mono'>{a:.1f}s</span> {esc(x)}</p>" for a, z, x in caps)
    return f"""<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Reel Check · {esc(M['file'])}</title>
<style>:root{{--a:#3a9ed2;--ink:#323232;--muted:#5b6670;--card:#f3f5f8;--line:#d6e5ef}}*{{box-sizing:border-box}}body{{margin:0;font-family:Inter,system-ui,sans-serif;color:var(--ink);background:#fff}}
main{{max-width:980px;margin:0 auto;padding:32px 16px 48px;display:grid;gap:22px}}h1{{font-size:34px;letter-spacing:-.03em;margin:0}}h2{{font-size:20px;margin:0 0 10px}}h3{{font-size:16px}}
.k{{font-family:'IBM Plex Mono',monospace;font-size:12px;letter-spacing:.12em;color:var(--a);text-transform:uppercase}}.muted{{color:var(--muted)}}.mono{{font-family:'IBM Plex Mono',monospace;color:var(--muted);font-size:13px}}
.card{{background:var(--card);border-radius:16px;padding:18px}}.score{{font-size:44px;font-weight:800;color:var(--a)}}table{{width:100%;border-collapse:collapse}}td{{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}}
tr.ok td:first-child{{color:#1f8a4c;font-weight:800}}tr.ko td:first-child{{color:#c0392b;font-weight:800}}tr.nd td:first-child{{color:var(--muted)}}.strip{{display:flex;gap:8px;overflow-x:auto}}figure{{margin:0}}figure img{{height:200px;border-radius:10px;display:block}}figcaption{{font-size:12px;color:var(--muted);text-align:center}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.grid div{{background:#fff;border-radius:12px;padding:12px}}.grid b{{display:block;font-size:22px}}pre{{white-space:pre-wrap;background:#fff;border-radius:10px;padding:12px;font-family:inherit}}</style></head>
<body><main><div><div class='k'>Creattiva · Reel Check</div><h1>{esc(M['file'])}</h1><p class='muted'>Analizzato il {esc(M['analizzato'])}. Misure reali dal file e controlli sulle regole editoriali di Creattiva: nessuna stima di viralità.</p></div>
<div class='card'><span class='score'>{ok}/{tot}</span> <span class='muted'>controlli superati</span><table>{rows}</table></div>
<div class='card'><h2>Misure</h2><div class='grid'><div><b>{M['durata']:.1f} s</b>durata</div><div><b>{M['cambi_visivi']}</b>cambi visivi</div><div><b>{M['movimento_0_3s']}</b>movimento 0–3 s<br><span class='mono'>media {M['movimento_medio']}</span></div><div><b>{M['lufs_integrato']}</b>LUFS</div><div><b>{M['parole_al_minuto']}</b>parole/min</div><div><b>{M['prima_parola_s']} s</b>prima parola</div></div></div>
<div class='card'><h2>Ritmo visivo</h2><p class='mono'>Differenza tra fotogrammi; linee tratteggiate = cambi visivi</p>{spark(t, diff, 900, 90, 0, max(30, float(diff.max()) if len(diff) else 30), '#323232', cuts)}
<h2 style='margin-top:14px'>Volume</h2><p class='mono'>Loudness momentanea (LUFS)</p>{spark(lt, lm, 900, 70, -50, -5, '#3a9ed2')}</div>
<div class='card'><h2>Fotogrammi</h2><div class='strip'>{imgs}</div></div>
<div class='card'><h2>Gancio dei primi 3 secondi</h2><p><b>{esc(M['gancio_primi_3s'] or 'non disponibile')}</b></p><p class='mono'>domanda: {'sì' if M['gancio_ha_domanda'] else 'no'} · numero: {'sì' if M['gancio_ha_numero'] else 'no'}</p>{trans}</div>
{('<div class="card"><h2>Proposte del modello locale</h2>'+sg+'</div>') if sg else ''}
<p class='mono'>Le soglie sono regole editoriali di Creattiva, non regole delle piattaforme. Quando lo storico avrà almeno 10 post con i loro risultati, il comando learn dirà quali misure contano davvero per noi.</p></main></body></html>"""


def normalize(args):
    """Copia del video con volume portato a -14 LUFS (due passate di loudnorm), video invariato."""
    v = args.video
    out = args.output or os.path.splitext(v)[0] + "-14lufs.mp4"
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", v, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
    af = (f"loudnorm=I=-14:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    run(["ffmpeg", "-v", "error", "-y", "-i", v, "-c:v", "copy", "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out])
    print(f"Volume da {m['input_i']} a circa -14 LUFS: {out}")


def learn(args):
    hist = list(csv.DictReader(open(args.history, encoding="utf-8")))
    met = {r["file"]: r for r in csv.DictReader(open(args.metrics, encoding="utf-8"))}
    rows = [(h, met[h["file"]]) for h in hist if h["file"] in met and met[h["file"]].get(args.metric, "") not in ("", None)]
    print(f"Post con misure e risultati: {len(rows)}")
    if len(rows) < 10:
        print("Servono almeno 10 post per un confronto minimo: per ora nessuna conclusione."); return
    y = np.array([float(m[args.metric]) for _, m in rows])
    rank = lambda a: np.argsort(np.argsort(a)).astype(float)
    feats = ["durata", "cambi_visivi", "primo_cambio_s", "intervallo_medio_cambi_s", "movimento_0_3s", "lufs_integrato",
             "prima_parola_s", "parole_al_minuto", "silenzio_piu_lungo_s", "hashtag"]
    out = []
    for f in feats:
        try:
            x = np.array([float(h[f]) for h, _ in rows])
        except (ValueError, KeyError):
            continue
        if np.std(x) == 0: continue
        r = float(np.corrcoef(rank(x), rank(y))[0, 1]); out.append((abs(r), f, r))
    print(f"Correlazione di rango con '{args.metric}' (da -1 a +1; con pochi post è solo un indizio, non una causa):")
    for _, f, r in sorted(out, reverse=True):
        print(f"  {f:28s} {r:+.2f}")


def main():
    ap = argparse.ArgumentParser(description="Reel Check di Creattiva")
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("analyze"); a.add_argument("video"); a.add_argument("--srt"); a.add_argument("--caption")
    a.add_argument("--out", default="report"); a.add_argument("--history", default="storico.csv"); a.add_argument("--ollama-model")
    n = sp.add_parser("normalize"); n.add_argument("video"); n.add_argument("--output")
    l = sp.add_parser("learn"); l.add_argument("metrics"); l.add_argument("--metric", default="views"); l.add_argument("--history", default="storico.csv")
    args = ap.parse_args()
    {"analyze": analyze, "normalize": normalize, "learn": learn}[args.cmd](args)


if __name__ == "__main__":
    main()
