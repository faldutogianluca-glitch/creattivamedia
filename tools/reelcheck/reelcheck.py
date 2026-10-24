#!/usr/bin/env python3
"""Reel Check di Creattiva (versione 2): controlli tecnici di un video breve prima di pubblicarlo.

Cosa fa
  analyze    misura il file (formato, ritmo visivo, audio, parlato dai sottotitoli) e lo confronta con le
             regole editoriali di Creattiva; salva una riga nello storico con identificativi distinti
             (asset, versione, rilevazione).
  normalize  crea una copia con il volume a -14 LUFS (due passate di loudnorm), poi rimisura il risultato.
  learn      confronta le misure con i risultati reali per pubblicazione (asset × piattaforma × post):
             correlazioni di rango ESPLORATIVE, mai previsioni di viralita'.

Cosa non fa
  Non giudica il contenuto: le verifiche editoriali (affermazioni con fonte, approvazione, una sola CTA
  coerente con l'offerta) restano umane e il report le elenca come "da confermare a mano".

Requisiti: Python 3.9+, numpy, Pillow, ffmpeg/ffprobe nel PATH.
"""
import argparse, base64, csv, hashlib, io, json, os, re, subprocess, sys, urllib.request, uuid
from datetime import datetime, timezone
import numpy as np

VERSION = "2.0.0"

# Regole editoriali di Creattiva: soglie scelte da noi, non regole delle piattaforme.
RULES = {
    "durata_min": 15, "durata_max": 60,
    "prima_parola_max": 1.0,
    "primo_cambio_max": 3.0,
    "intervallo_cambi_max": 5.0,
    "silenzio_max": 0.7,
    "lufs_min": -18.0, "lufs_max": -10.0,
    "wpm_min": 130, "wpm_max": 190,
    "lufs_target": -14.0, "lufs_tolleranza": 1.5,
}
CTA_VERBS = [r"\bcommentate\b", r"\bcommenta\b", r"\bscrivete\b", r"\bscrivi\b", r"\bcliccate\b",
             r"\biscrivetevi\b", r"\bprenotate\b", r"\bprenota\b", r"\bcandidatevi\b", r"\bscaricate\b"]
CTA_CONTEXT = [r"\blink\b", r"\bin privato\b", r"\bdirect\b", r"\bprimo commento\b"]
NOT_KEYWORDS = {"CREATTIVA", "LUFS", "AI", "PMI", "FAQ", "PDF", "CRM", "ERP", "LUL", "DURC", "EQF", "FSE", "SIUF", "IVA"}
AI_NOTE = re.compile(r"ausilio dell.?AI|generat\w+ con (l.?)?AI|avatar AI|supervisione di", re.I)


class ReelCheckError(RuntimeError):
    pass


def run(cmd, what):
    """Esegue un comando esterno; se fallisce solleva un errore con lo stderr (niente successi finti)."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise ReelCheckError(f"{what}: programma non trovato ({e.filename}). Serve ffmpeg/ffprobe nel PATH.")
    if p.returncode != 0:
        tail = (p.stderr or "").strip().splitlines()[-3:]
        raise ReelCheckError(f"{what}: ffmpeg ha restituito errore {p.returncode}: " + " | ".join(tail))
    return p


def same_path(a, b):
    return os.path.realpath(os.path.abspath(a)) == os.path.realpath(os.path.abspath(b))


def asset_id_for(path):
    """Identificativo dell'asset: impronta del contenuto, cosi' lo stesso file ha sempre lo stesso ID
    e due file con lo stesso nome ma contenuto diverso hanno ID diversi."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def probe(path):
    if not os.path.isfile(path):
        raise ReelCheckError(f"file non trovato: {path}")
    out = json.loads(run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path], "ffprobe").stdout)
    vs = [s for s in out.get("streams", []) if s.get("codec_type") == "video"]
    if not vs:
        raise ReelCheckError("nessuna traccia video nel file")
    v = vs[0]
    a = [s for s in out["streams"] if s["codec_type"] == "audio"]
    num, den = (int(x) for x in v.get("avg_frame_rate", "0/1").split("/"))
    return {"durata": float(out["format"]["duration"]), "larghezza": int(v["width"]), "altezza": int(v["height"]),
            "fps": round(num / den, 2) if den else 0, "audio": bool(a)}


def motion(path, info, fps=10):
    """Differenza media tra fotogrammi consecutivi (0-255), a 10 fotogrammi al secondo."""
    w = 96
    h = int(round(info["altezza"] * w / info["larghezza"] / 2) * 2)
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={w}:{h},format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelCheckError("estrazione fotogrammi: " + p.stderr.decode(errors="ignore")[-300:])
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


def integrated_lufs(path):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "ebur128", "-f", "null", "-"], "misura loudness")
    integ = re.findall(r"I:\s*(-?[\d.]+) LUFS", r.stderr)
    return float(integ[-1]) if integ else None


def loudness(path):
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "verbose", "-i", path, "-af", "ebur128", "-f", "null", "-"], "misura loudness")
    ts, ms = [], []
    for m in re.finditer(r"t:\s*([\d.]+)\s+TARGET.*?M:\s*(-?[\d.]+|-inf)", r.stderr):
        ts.append(float(m.group(1))); ms.append(-70.0 if m.group(2) == "-inf" else float(m.group(2)))
    integ = re.findall(r"I:\s*(-?[\d.]+) LUFS", r.stderr)
    s = run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-af", "silencedetect=noise=-35dB:d=0.4", "-f", "null", "-"], "silencedetect").stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", s)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", s)]
    gaps = [(round(a, 2), round(b - a, 2)) for a, b in zip(starts, ends)]
    first_sound = next((t for t, m in zip(ts, ms) if m > -40), None)
    return np.array(ts), np.array(ms), (float(integ[-1]) if integ else None), gaps, first_sound


def read_srt(path):
    def sec(x):
        h, m, s = x.split(":"); s, ms = s.split(","); return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    caps = []
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    for b in text.strip().split("\n\n"):
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
        if p.returncode == 0 and p.stdout:
            out.append((round(t, 1), base64.b64encode(p.stdout).decode()))
    return out


def cta_summary(text):
    """Conta i richiami all'azione: verbi di invito e parole chiave in maiuscolo (CORSO, LOCALE, CHECK-UP)."""
    verbs = sum(len(re.findall(p, text, re.I)) for p in CTA_VERBS)
    context = sum(len(re.findall(p, text, re.I)) for p in CTA_CONTEXT)
    keywords = sorted(set(re.findall(r"\b[A-Z][A-Z\-]{3,}\b", text)) - NOT_KEYWORDS)
    return {"verbi": verbs, "contesto": context, "parole_chiave": keywords}


def cta_check(text):
    """Esito del controllo "una sola chiamata all'azione".
    - nessun verbo di invito e nessuna parola chiave  -> da rivedere (manca la CTA)
    - piu' di una parola chiave (CORSO e LOCALE insieme) -> da rivedere (CTA diverse)
    - altrimenti ok. Il controllo e' lessicale: la coerenza con l'offerta resta una verifica umana."""
    s = cta_summary(text)
    if s["verbi"] == 0 and not s["parole_chiave"]:
        return False, "nessuna chiamata all'azione trovata", s
    if len(s["parole_chiave"]) > 1:
        return False, "parole chiave diverse: " + ", ".join(s["parole_chiave"]), s
    if s["verbi"] > 3:
        return False, f"{s['verbi']} inviti: troppi per un video breve", s
    return True, f"{s['verbi']} inviti, parola chiave {s['parole_chiave'][0] if s['parole_chiave'] else 'nessuna'}", s


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


HISTORY_FIELDS = ["rilevazione_id", "asset_id", "versione", "file", "percorso", "analizzato", "reelcheck_versione",
                  "durata", "larghezza", "altezza", "fps", "audio", "verticale_9x16", "cambi_visivi", "primo_cambio_s",
                  "intervallo_medio_cambi_s", "movimento_0_3s", "movimento_medio", "lufs_integrato", "primo_suono_s",
                  "silenzio_piu_lungo_s", "prima_parola_s", "parole", "parole_al_minuto", "gancio_ha_domanda",
                  "gancio_ha_numero", "cta_verbi", "cta_parole_chiave", "nota_ai_in_didascalia", "hashtag", "controlli_ok", "controlli_totali"]


def measure(video, srt=None, caption_path=None):
    info = probe(video)
    t, diff, cuts = motion(video, info)
    lt, lm, lufs, gaps, first_sound = loudness(video) if info["audio"] else (np.array([]), np.array([]), None, [], None)
    caps = read_srt(srt) if srt else []
    caption = ""
    if caption_path:
        with open(caption_path, encoding="utf-8") as fh:
            caption = fh.read()
    dur = info["durata"]
    transcript = " ".join(c[2] for c in caps)
    words = len(transcript.split())
    speech_time = sum(z - a for a, z, _ in caps)
    wpm = round(words / speech_time * 60) if speech_time else None
    # La prima parola si legge SOLO dai sottotitoli. Il primo suono e' un'altra misura (puo' essere musica o rumore).
    first_word = round(caps[0][0], 2) if caps else None
    hook = " ".join(c[2] for c in caps if c[0] < 3.0)
    intervals = np.diff([0.0] + cuts + [dur]) if cuts else np.array([dur])
    inner_gaps = [g for g in gaps if g[0] > 0.5 and g[0] + g[1] < dur - 0.5]
    longest_gap = max((g[1] for g in inner_gaps), default=0.0)
    cta_ok, cta_detail, cta_s = cta_check(transcript + " " + caption) if (caption or caps) else (None, "parlato e didascalia non forniti", cta_summary(""))
    M = {
        "file": os.path.basename(video), "percorso": os.path.abspath(video),
        "analizzato": datetime.now().astimezone().isoformat(timespec="seconds"), "reelcheck_versione": VERSION,
        **info, "verticale_9x16": abs(info["altezza"] / info["larghezza"] - 16 / 9) < 0.02,
        "cambi_visivi": len(cuts), "primo_cambio_s": cuts[0] if cuts else None,
        "intervallo_medio_cambi_s": round(float(intervals.mean()), 2),
        "movimento_0_3s": round(float(diff[t <= 3].mean()), 2) if len(diff) and (t <= 3).any() else None,
        "movimento_medio": round(float(diff.mean()), 2) if len(diff) else None,
        "lufs_integrato": lufs, "primo_suono_s": round(first_sound, 2) if first_sound is not None else None,
        "silenzio_piu_lungo_s": longest_gap,
        "prima_parola_s": first_word, "parole": words, "parole_al_minuto": wpm,
        "gancio_primi_3s": hook, "gancio_ha_domanda": "?" in hook, "gancio_ha_numero": bool(re.search(r"\d|cento|mille", hook, re.I)),
        "cta_verbi": cta_s["verbi"], "cta_parole_chiave": " ".join(cta_s["parole_chiave"]),
        "nota_ai_in_didascalia": bool(AI_NOTE.search(caption)) if caption else None,
        "hashtag": len(re.findall(r"#\w+", caption)),
    }
    R = RULES
    def chk(name, ok, detail):
        return {"controllo": name, "esito": "n.d." if ok is None else ("ok" if ok else "da rivedere"), "dettaglio": detail}
    tecnici = [
        chk("Formato verticale 9:16", M["verticale_9x16"], f"{info['larghezza']}×{info['altezza']}"),
        chk(f"Durata tra {R['durata_min']} e {R['durata_max']} s", R["durata_min"] <= dur <= R["durata_max"], f"{dur:.1f} s"),
        chk(f"Prima parola entro {R['prima_parola_max']:.0f} s (dai sottotitoli)", None if first_word is None else first_word <= R["prima_parola_max"],
            f"{first_word} s" if first_word is not None else ("senza .srt non si misura; il primo suono è a " + (f"{M['primo_suono_s']} s" if M["primo_suono_s"] is not None else "n.d.") + " e non è necessariamente una parola")),
        chk(f"Un cambio visivo entro {R['primo_cambio_max']:.0f} s", None if not len(diff) else bool(cuts and cuts[0] <= R["primo_cambio_max"]), f"primo cambio a {cuts[0]} s" if cuts else "nessun cambio rilevato"),
        chk(f"Un cambio visivo almeno ogni {R['intervallo_cambi_max']:.0f} s in media", M["intervallo_medio_cambi_s"] <= R["intervallo_cambi_max"], f"{len(cuts)} cambi, uno ogni {M['intervallo_medio_cambi_s']} s"),
        chk(f"Nessun silenzio oltre {R['silenzio_max']} s", None if not info["audio"] else longest_gap <= R["silenzio_max"], f"pausa più lunga {longest_gap} s"),
        chk(f"Volume tra {R['lufs_min']:.0f} e {R['lufs_max']:.0f} LUFS", None if lufs is None else R["lufs_min"] <= lufs <= R["lufs_max"], f"{lufs} LUFS" if lufs is not None else "n.d."),
        chk(f"Ritmo parlato tra {R['wpm_min']} e {R['wpm_max']} parole/min", None if wpm is None else R["wpm_min"] <= wpm <= R["wpm_max"], f"{wpm} parole/min" if wpm else "serve il file dei sottotitoli"),
        chk("Sottotitoli disponibili", bool(caps) if srt else None, "file .srt fornito" if caps else "non verificato: fornite il file .srt"),
        chk("Una sola chiamata all'azione (controllo lessicale)", cta_ok, cta_detail),
        chk("Nota sull'uso dell'AI in didascalia", M["nota_ai_in_didascalia"], "presente" if M["nota_ai_in_didascalia"] else ("mancante" if caption else "didascalia non fornita")),
    ]
    editoriali = [
        {"verifica": "Ogni affermazione con numeri o regole ha una fonte nel manifesto del video", "esito": "da confermare a mano"},
        {"verifica": "La chiamata all'azione è coerente con l'offerta e con gli altri post dello stesso giorno (mai due CTA diverse sulla stessa pagina)", "esito": "da confermare a mano"},
        {"verifica": "Nessun numero, cliente o risultato inventato; gli esempi sono dichiarati come esempi", "esito": "da confermare a mano"},
        {"verifica": "Volto e voce corrispondono alla persona dichiarata (vedi manifesto: look e voice id)", "esito": "da confermare a mano"},
        {"verifica": "Approvazione scritta di Gianluca Falduto prima della programmazione", "esito": "da confermare a mano"},
    ]
    return M, tecnici, editoriali, {"t": t, "diff": diff, "cuts": cuts, "lt": lt, "lm": lm, "caps": caps, "caption": caption, "transcript": transcript}


def analyze(args):
    v = args.video
    if args.out and os.path.isfile(args.out):
        raise ReelCheckError("--out deve essere una cartella, non un file")
    M, tecnici, editoriali, X = measure(v, args.srt, args.caption)
    asset = args.asset_id or asset_id_for(v)
    M.update({"asset_id": asset, "versione": args.version or "", "rilevazione_id": uuid.uuid4().hex[:10]})
    sugg = ollama(args.ollama_model, X["transcript"], X["caption"]) if args.ollama_model else None
    os.makedirs(args.out, exist_ok=True)
    stem = os.path.splitext(os.path.basename(v))[0]
    base = os.path.join(args.out, f"{stem}-{asset}-reelcheck")
    for ext in (".json", ".html"):
        if same_path(base + ext, v):
            raise ReelCheckError("il report coinciderebbe con il file di ingresso")
    ok = sum(c["esito"] == "ok" for c in tecnici); tot = sum(c["esito"] != "n.d." for c in tecnici)
    M["controlli_ok"], M["controlli_totali"] = ok, tot
    payload = {"identificativi": {"asset_id": asset, "versione": M["versione"], "rilevazione_id": M["rilevazione_id"]},
               "misure": M, "controlli_tecnici": tecnici, "verifiche_editoriali": editoriali,
               "risultati_commerciali": "non disponibili all'analisi: si leggono con il comando learn dai dati per pubblicazione",
               "suggerimenti": sugg}
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(report_html(M, tecnici, editoriali, X["t"], X["diff"], X["cuts"], X["lt"], X["lm"], X["caps"], frames_strip(v, M["durata"]), sugg))
    append_history(args.history, M)
    print(f"Reel Check {VERSION} · asset {asset} · rilevazione {M['rilevazione_id']}")
    print(f"Controlli tecnici superati: {ok}/{tot}")
    for c in tecnici: print(f"  [{c['esito']:^12}] {c['controllo']}: {c['dettaglio']}")
    print("Verifiche editoriali (umane): " + "; ".join(e["verifica"] for e in editoriali))
    print(f"Report: {base}.html\nStorico aggiornato: {args.history}")
    return payload


def append_history(hist, M):
    new = not os.path.exists(hist)
    with open(hist, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS, extrasaction="ignore")
        if new: w.writeheader()
        w.writerow(M)


def spark(xs, ys, w, h, lo, hi, color, marks=()):
    if len(xs) == 0: return ""
    X = lambda x: x / max(xs[-1], 1e-6) * w
    Y = lambda y: h - (min(max(y, lo), hi) - lo) / (hi - lo) * h
    pts = " ".join(f"{X(x):.1f},{Y(y):.1f}" for x, y in zip(xs, ys))
    m = "".join(f"<line x1='{X(c):.1f}' x2='{X(c):.1f}' y1='0' y2='{h}' stroke='#3a9ed2' stroke-dasharray='3 3'/>" for c in marks)
    return f"<svg viewBox='0 0 {w} {h}' preserveAspectRatio='none' style='width:100%;height:{h}px'>{m}<polyline points='{pts}' fill='none' stroke='{color}' stroke-width='1.6'/></svg>"


def report_html(M, C, E, t, diff, cuts, lt, lm, caps, strip, sugg):
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    ok = sum(c["esito"] == "ok" for c in C); tot = sum(c["esito"] != "n.d." for c in C)
    rows = "".join(f"<tr class='{ 'ok' if c['esito']=='ok' else ('nd' if c['esito']=='n.d.' else 'ko')}'><td>{ '✓' if c['esito']=='ok' else ('–' if c['esito']=='n.d.' else '✗')}</td><td>{esc(c['controllo'])}</td><td>{esc(c['dettaglio'])}</td></tr>" for c in C)
    erows = "".join(f"<tr class='nd'><td>☐</td><td>{esc(e['verifica'])}</td><td>{esc(e['esito'])}</td></tr>" for e in E)
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
<body><main><div><div class='k'>Creattiva · Reel Check {VERSION}</div><h1>{esc(M['file'])}</h1><p class='muted'>Asset {esc(M.get('asset_id',''))} · versione {esc(M.get('versione','') or '–')} · rilevazione {esc(M.get('rilevazione_id',''))} · analizzato il {esc(M['analizzato'])}.<br>Misure reali dal file e controlli sulle regole editoriali di Creattiva: nessuna stima di viralità.</p></div>
<div class='card'><h2>Controlli tecnici (automatici)</h2><span class='score'>{ok}/{tot}</span> <span class='muted'>superati</span><table>{rows}</table></div>
<div class='card'><h2>Verifiche editoriali (umane, da confermare prima della programmazione)</h2><table>{erows}</table></div>
<div class='card'><h2>Risultati commerciali</h2><p class='muted'>Non si leggono qui: arrivano dalle piattaforme per pubblicazione (asset × piattaforma × post) e si confrontano con il comando <span class='mono'>learn</span>, in modo esplorativo.</p></div>
<div class='card'><h2>Misure</h2><div class='grid'><div><b>{M['durata']:.1f} s</b>durata</div><div><b>{M['cambi_visivi']}</b>cambi visivi</div><div><b>{M['movimento_0_3s']}</b>movimento 0–3 s<br><span class='mono'>media {M['movimento_medio']}</span></div><div><b>{M['lufs_integrato']}</b>LUFS</div><div><b>{M['parole_al_minuto']}</b>parole/min</div><div><b>{M['prima_parola_s']} s</b>prima parola (srt)</div><div><b>{M['primo_suono_s']} s</b>primo suono</div></div></div>
<div class='card'><h2>Ritmo visivo</h2><p class='mono'>Differenza tra fotogrammi; linee tratteggiate = cambi visivi</p>{spark(t, diff, 900, 90, 0, max(30, float(diff.max()) if len(diff) else 30), '#323232', cuts)}
<h2 style='margin-top:14px'>Volume</h2><p class='mono'>Loudness momentanea (LUFS)</p>{spark(lt, lm, 900, 70, -50, -5, '#3a9ed2')}</div>
<div class='card'><h2>Fotogrammi</h2><div class='strip'>{imgs}</div></div>
<div class='card'><h2>Gancio dei primi 3 secondi</h2><p><b>{esc(M['gancio_primi_3s'] or 'non disponibile')}</b></p><p class='mono'>domanda: {'sì' if M['gancio_ha_domanda'] else 'no'} · numero: {'sì' if M['gancio_ha_numero'] else 'no'}</p>{trans}</div>
{('<div class="card"><h2>Proposte del modello locale</h2>'+sg+'</div>') if sg else ''}
<p class='mono'>Le soglie sono regole editoriali di Creattiva, non regole delle piattaforme. Le correlazioni del comando learn sono esplorative: con pochi post sono solo indizi, mai cause.</p></main></body></html>"""


def normalize(args):
    """Copia del video con volume portato a -14 LUFS (due passate di loudnorm), video invariato.
    Rifiuta di sovrascrivere l'ingresso o un'uscita esistente; alla fine rimisura il file prodotto."""
    v = args.video
    if not os.path.isfile(v):
        raise ReelCheckError(f"file non trovato: {v}")
    out = args.output or os.path.splitext(v)[0] + "-14lufs.mp4"
    if same_path(v, out):
        raise ReelCheckError("ingresso e uscita coincidono: scegliete un --output diverso")
    if os.path.exists(out) and not args.overwrite:
        raise ReelCheckError(f"l'uscita esiste già: {out} (usate --overwrite per sostituirla)")
    target = RULES["lufs_target"]
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", v, "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], "misura loudnorm")
    try:
        m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
    except (ValueError, KeyError) as e:
        raise ReelCheckError(f"non leggo la misura di loudnorm ({e.__class__.__name__}); il file ha una traccia audio?")
    af = (f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    tmp = out + ".part.mp4"
    try:
        run(["ffmpeg", "-v", "error", "-y", "-i", v, "-c:v", "copy", "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", tmp], "normalizzazione")
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp): os.remove(tmp)
    after = integrated_lufs(out)
    ok = after is not None and abs(after - target) <= RULES["lufs_tolleranza"]
    print(f"Volume: da {m['input_i']} LUFS a {after} LUFS misurati dopo la normalizzazione (obiettivo {target}): {'ok' if ok else 'da rivedere'}")
    print(f"Uscita: {out}")
    if not ok:
        raise ReelCheckError("il ricontrollo dopo la normalizzazione non rientra nella tolleranza")
    return out, m["input_i"], after


def rankdata(a):
    """Ranghi con media sui pari merito (come scipy.stats.rankdata, metodo 'average')."""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 3 or np.ptp(x) == 0 or np.ptp(y) == 0:
        return None
    rx, ry = rankdata(x), rankdata(y)
    return float(np.corrcoef(rx, ry)[0, 1])


def parse_dt(s):
    if not s: return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


RESULT_KEYS = ("asset_id", "piattaforma", "post_id")


def load_results(path):
    """Risultati per pubblicazione. Colonne attese: asset_id, piattaforma, post_id, pubblicato_il, rilevato_il,
    distribuzione (organica|pagata), piu' le metriche (views, impressions, comments, clicks, ...).
    Piu' rilevazioni della stessa pubblicazione: si tiene l'ultima per rilevato_il."""
    with open(path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    missing = [k for k in RESULT_KEYS if rows and k not in rows[0]]
    if missing:
        raise ReelCheckError("nel file dei risultati mancano le colonne: " + ", ".join(missing) + ". Ogni riga è una pubblicazione (asset × piattaforma × post).")
    best = {}
    for r in rows:
        key = tuple((r.get(k) or "").strip() for k in RESULT_KEYS)
        if not all(key): continue
        prev = best.get(key)
        if prev is None or (parse_dt(r.get("rilevato_il")) or datetime.min.replace(tzinfo=timezone.utc)) >= (parse_dt(prev.get("rilevato_il")) or datetime.min.replace(tzinfo=timezone.utc)):
            best[key] = r
    return best


def latest_history(path):
    """Ultima analisi per asset_id (le analisi ripetute dello stesso file non diventano post distinti)."""
    with open(path, encoding="utf-8") as fh:
        hist = list(csv.DictReader(fh))
    if hist and "asset_id" not in hist[0]:
        raise ReelCheckError("storico in formato vecchio (senza asset_id): rianalizzate i video con questa versione")
    latest = {}
    for h in hist:
        a = h.get("asset_id", "")
        if a and (a not in latest or h.get("analizzato", "") >= latest[a].get("analizzato", "")):
            latest[a] = h
    return latest


FEATURES = ["durata", "cambi_visivi", "primo_cambio_s", "intervallo_medio_cambi_s", "movimento_0_3s", "lufs_integrato",
            "prima_parola_s", "parole_al_minuto", "silenzio_piu_lungo_s", "hashtag", "cta_verbi"]


def learn(args):
    hist = latest_history(args.history)
    pubs = load_results(args.metrics)
    rows, skipped = [], {"pagate": 0, "altra_piattaforma": 0, "senza_misure": 0, "metrica_mancante": 0, "senza_date": 0}
    for (asset, plat, post), r in pubs.items():
        if args.platform and plat.lower() != args.platform.lower():
            skipped["altra_piattaforma"] += 1; continue
        if (r.get("distribuzione") or "organica").strip().lower() != "organica" and not args.include_paid:
            skipped["pagate"] += 1; continue
        if asset not in hist:
            skipped["senza_misure"] += 1; continue
        try:
            y = float(r[args.metric])
        except (KeyError, TypeError, ValueError):
            skipped["metrica_mancante"] += 1; continue
        if args.per_day:
            p, d = parse_dt(r.get("pubblicato_il")), parse_dt(r.get("rilevato_il"))
            if not p or not d:
                skipped["senza_date"] += 1; continue
            y = y / max((d - p).total_seconds() / 86400, 1.0)
        rows.append((hist[asset], y, plat, post))
    n = len(rows)
    print(f"Pubblicazioni con misure e risultati: {n} (scartate: {', '.join(f'{k} {v}' for k, v in skipped.items() if v)})" if any(skipped.values()) else f"Pubblicazioni con misure e risultati: {n}")
    plats = sorted({p for _, _, p, _ in rows})
    if len(plats) > 1:
        print(f"Attenzione: piattaforme diverse insieme ({', '.join(plats)}): i denominatori non sono confrontabili. Usate --platform per una lettura per piattaforma.")
    if n < args.min_n:
        print(f"Servono almeno {args.min_n} pubblicazioni per un confronto minimo: per ora nessuna conclusione."); return []
    y = np.array([v for _, v, _, _ in rows])
    if np.ptp(y) == 0:
        print(f"Risultati tutti uguali ({args.metric} = {y[0]:g} per tutte): nessuna correlazione calcolabile. Controllate il file dei risultati."); return []
    out = []
    for f in FEATURES:
        xs = []
        for h, _, _, _ in rows:
            try: xs.append(float(h[f]))
            except (KeyError, TypeError, ValueError): xs.append(np.nan)
        x = np.array(xs)
        mask = ~np.isnan(x)
        if mask.sum() < args.min_n: continue
        r = spearman(x[mask], y[mask])
        if r is None: continue
        out.append((abs(r), f, r, int(mask.sum())))
    print(f"Correlazione di rango (Spearman, pari merito a rango medio) con '{args.metric}'{' al giorno' if args.per_day else ''}: esplorativa, non causale, nessuna previsione di viralità.")
    for _, f, r, k in sorted(out, reverse=True):
        print(f"  {f:28s} {r:+.2f}  (n={k})")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reel Check di Creattiva")
    ap.add_argument("--version", action="version", version=VERSION)
    sp = ap.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("analyze"); a.add_argument("video"); a.add_argument("--srt"); a.add_argument("--caption")
    a.add_argument("--out", default="report"); a.add_argument("--history", default="storico.csv"); a.add_argument("--ollama-model")
    a.add_argument("--asset-id", help="identificativo dell'asset (default: impronta del contenuto)"); a.add_argument("--version-tag", dest="version", help="versione dell'asset, es. v2-14lufs")
    n = sp.add_parser("normalize"); n.add_argument("video"); n.add_argument("--output"); n.add_argument("--overwrite", action="store_true")
    l = sp.add_parser("learn"); l.add_argument("metrics"); l.add_argument("--metric", default="views"); l.add_argument("--history", default="storico.csv")
    l.add_argument("--platform"); l.add_argument("--per-day", action="store_true"); l.add_argument("--include-paid", action="store_true"); l.add_argument("--min-n", type=int, default=10)
    args = ap.parse_args(argv)
    try:
        return {"analyze": analyze, "normalize": normalize, "learn": learn}[args.cmd](args)
    except ReelCheckError as e:
        print(f"ERRORE: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
