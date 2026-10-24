"""Test di regressione di Reel Check, mirati ai difetti reali trovati nella versione 1.
Esecuzione: python3 -m unittest discover -s tests -v   (serve ffmpeg nel PATH)"""
import csv, io, json, os, shutil, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import reelcheck as rc  # noqa: E402

FFMPEG = shutil.which("ffmpeg") is not None


def make_video(path, seconds=16, silent_start=0.0, tone=True):
    """Video sintetico piccolo: mire TV + tono a 440 Hz (eventualmente dopo un silenzio iniziale)."""
    audio = f"sine=frequency=440:duration={seconds}" if tone else f"anullsrc=r=48000:cl=mono:d={seconds}"
    af = ["-af", f"adelay={int(silent_start*1000)}|{int(silent_start*1000)}"] if silent_start else []
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=180x320:rate=10",
           "-f", "lavfi", "-i", audio, *af, "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", path]
    subprocess.run(cmd, check=True)


class Args:
    def __init__(self, **kw):
        self.__dict__.update(dict(video=None, srt=None, caption=None, out="report", history="storico.csv", ollama_model=None,
                                  asset_id=None, version=None, output=None, overwrite=False, metrics=None, metric="views",
                                  platform=None, per_day=False, include_paid=False, min_n=10))
        self.__dict__.update(kw)


class TestRanksAndLearn(unittest.TestCase):
    def test_ranghi_pari_merito_rango_medio(self):
        self.assertEqual(list(rc.rankdata([10, 20, 20, 30])), [1.0, 2.5, 2.5, 4.0])
        self.assertEqual(list(rc.rankdata([5, 5, 5])), [2.0, 2.0, 2.0])

    def test_spearman_dati_costanti_restituisce_none(self):
        self.assertIsNone(rc.spearman([1, 2, 3, 4], [7, 7, 7, 7]))
        self.assertIsNone(rc.spearman([3, 3, 3, 3], [1, 2, 3, 4]))
        self.assertAlmostEqual(rc.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)

    def _write(self, d, hist_rows, res_rows):
        hist = os.path.join(d, "storico.csv"); res = os.path.join(d, "risultati.csv")
        with open(hist, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rc.HISTORY_FIELDS, extrasaction="ignore"); w.writeheader()
            for r in hist_rows: w.writerow(r)
        with open(res, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["asset_id", "piattaforma", "post_id", "pubblicato_il", "rilevato_il", "distribuzione", "views"]); w.writeheader()
            for r in res_rows: w.writerow(r)
        return hist, res

    def test_risultati_tutti_uguali_nessuna_correlazione(self):
        with tempfile.TemporaryDirectory() as d:
            hist = [dict(asset_id=f"a{i}", analizzato=f"2026-10-0{i%9+1}T10:00:00", durata=10 + i, cambi_visivi=i) for i in range(12)]
            res = [dict(asset_id=f"a{i}", piattaforma="linkedin", post_id=f"p{i}", pubblicato_il="2026-10-01", rilevato_il="2026-10-10", distribuzione="organica", views=42) for i in range(12)]
            h, r = self._write(d, hist, res)
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = rc.learn(Args(metrics=r, history=h))
            self.assertEqual(out, [])
            self.assertIn("tutti uguali", buf.getvalue())

    def test_analisi_ripetute_stesso_asset_contano_una_volta(self):
        with tempfile.TemporaryDirectory() as d:
            hist = [dict(asset_id="a1", analizzato="2026-10-01T10:00:00", durata=10), dict(asset_id="a1", analizzato="2026-10-02T10:00:00", durata=11)]
            res = [dict(asset_id="a1", piattaforma="linkedin", post_id="p1", pubblicato_il="2026-10-01", rilevato_il="2026-10-10", distribuzione="organica", views=5)]
            h, r = self._write(d, hist, res)
            latest = rc.latest_history(h)
            self.assertEqual(len(latest), 1)
            self.assertEqual(latest["a1"]["durata"], "11")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc.learn(Args(metrics=r, history=h, min_n=1))
            self.assertIn("risultati: 1", buf.getvalue())

    def test_piattaforme_diverse_non_si_sovrascrivono(self):
        with tempfile.TemporaryDirectory() as d:
            hist = [dict(asset_id="a1", analizzato="2026-10-01T10:00:00", durata=10)]
            res = [dict(asset_id="a1", piattaforma="linkedin", post_id="p1", pubblicato_il="2026-10-01", rilevato_il="2026-10-10", distribuzione="organica", views=5),
                   dict(asset_id="a1", piattaforma="tiktok", post_id="t1", pubblicato_il="2026-10-01", rilevato_il="2026-10-10", distribuzione="organica", views=50),
                   dict(asset_id="a1", piattaforma="tiktok", post_id="t1", pubblicato_il="2026-10-01", rilevato_il="2026-10-12", distribuzione="organica", views=60)]
            h, r = self._write(d, hist, res)
            pubs = rc.load_results(r)
            self.assertEqual(len(pubs), 2, "due pubblicazioni distinte (linkedin, tiktok), non una")
            self.assertEqual(pubs[("a1", "tiktok", "t1")]["views"], "60", "si tiene l'ultima rilevazione della stessa pubblicazione")

    def test_pagate_escluse_e_per_giorno(self):
        with tempfile.TemporaryDirectory() as d:
            hist = [dict(asset_id=f"a{i}", analizzato="2026-10-01T10:00:00", durata=10 + i) for i in range(3)]
            res = [dict(asset_id="a0", piattaforma="linkedin", post_id="p0", pubblicato_il="2026-10-01T00:00:00+00:00", rilevato_il="2026-10-11T00:00:00+00:00", distribuzione="organica", views=100),
                   dict(asset_id="a1", piattaforma="linkedin", post_id="p1", pubblicato_il="2026-10-01T00:00:00+00:00", rilevato_il="2026-10-03T00:00:00+00:00", distribuzione="pagata", views=1000)]
            h, r = self._write(d, hist, res)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc.learn(Args(metrics=r, history=h, min_n=1, per_day=True))
            self.assertIn("pagate 1", buf.getvalue())

    def test_campione_insufficiente(self):
        with tempfile.TemporaryDirectory() as d:
            hist = [dict(asset_id="a1", analizzato="2026-10-01T10:00:00", durata=10)]
            res = [dict(asset_id="a1", piattaforma="linkedin", post_id="p1", pubblicato_il="2026-10-01", rilevato_il="2026-10-10", distribuzione="organica", views=5)]
            h, r = self._write(d, hist, res)
            buf = io.StringIO()
            with redirect_stdout(buf):
                out = rc.learn(Args(metrics=r, history=h))
            self.assertEqual(out, []); self.assertIn("almeno 10", buf.getvalue())


class TestCTA(unittest.TestCase):
    def test_nessuna_cta_non_passa(self):
        ok, detail, _ = rc.cta_check("Un video che spiega l'AI in locale. Fine.")
        self.assertFalse(ok); self.assertIn("nessuna", detail)

    def test_una_cta_passa(self):
        ok, _, _ = rc.cta_check("Commentate CORSO e vi mandiamo il programma.")
        self.assertTrue(ok)

    def test_due_parole_chiave_non_passano(self):
        ok, detail, _ = rc.cta_check("Commentate CORSO oppure scrivete LOCALE in privato.")
        self.assertFalse(ok); self.assertIn("CORSO", detail); self.assertIn("LOCALE", detail)

    def test_sigle_note_non_sono_cta(self):
        s = rc.cta_summary("Il DURC e il LUL servono alla Regione; CREATTIVA li verifica.")
        self.assertEqual(s["parole_chiave"], [])


@unittest.skipUnless(FFMPEG, "serve ffmpeg")
class TestVideo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = tempfile.mkdtemp()
        cls.v = os.path.join(cls.d, "clip.mp4"); make_video(cls.v, seconds=16)
        cls.late = os.path.join(cls.d, "clip-late.mp4"); make_video(cls.late, seconds=16, silent_start=2.0)
        cls.srt = os.path.join(cls.d, "clip.srt")
        open(cls.srt, "w").write("1\n00:00:00,400 --> 00:00:03,000\nCommentate CORSO per il programma\n\n2\n00:00:03,200 --> 00:00:06,000\nIl resto del parlato qui\n")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, ignore_errors=True)

    def test_senza_srt_prima_parola_non_e_il_primo_suono(self):
        M, tecnici, _, _ = rc.measure(self.late)
        self.assertIsNone(M["prima_parola_s"])
        self.assertIsNotNone(M["primo_suono_s"]); self.assertGreater(M["primo_suono_s"], 1.0)
        c = next(c for c in tecnici if c["controllo"].startswith("Prima parola"))
        self.assertEqual(c["esito"], "n.d."); self.assertIn("non è necessariamente una parola", c["dettaglio"])

    def test_con_srt_prima_parola_dai_sottotitoli(self):
        M, _, _, _ = rc.measure(self.v, srt=self.srt)
        self.assertEqual(M["prima_parola_s"], 0.4)

    def test_analyze_identificativi_e_storico(self):
        out = os.path.join(self.d, "rep"); hist = os.path.join(self.d, "st.csv")
        buf = io.StringIO()
        with redirect_stdout(buf):
            p1 = rc.analyze(Args(video=self.v, srt=self.srt, out=out, history=hist, version="v1"))
            p2 = rc.analyze(Args(video=self.v, srt=self.srt, out=out, history=hist, version="v1"))
        self.assertEqual(p1["identificativi"]["asset_id"], p2["identificativi"]["asset_id"], "stesso contenuto, stesso asset")
        self.assertNotEqual(p1["identificativi"]["rilevazione_id"], p2["identificativi"]["rilevazione_id"], "ogni analisi ha la sua rilevazione")
        rows = list(csv.DictReader(open(hist, encoding="utf-8").read().splitlines()))
        self.assertEqual(len(rows), 2); self.assertEqual(len(rc.latest_history(hist)), 1)
        self.assertIn("controlli_tecnici", p1); self.assertIn("verifiche_editoriali", p1)
        files = os.listdir(out); self.assertTrue(any(f.endswith(".html") for f in files))
        self.assertIn(p1["identificativi"]["asset_id"], files[0])

    def test_stesso_nome_contenuto_diverso_asset_diverso(self):
        sub = os.path.join(self.d, "altro"); os.makedirs(sub, exist_ok=True)
        other = os.path.join(sub, "clip.mp4"); make_video(other, seconds=15, tone=False)
        self.assertNotEqual(rc.asset_id_for(self.v), rc.asset_id_for(other))

    def test_normalize_rifiuta_stesso_percorso(self):
        with self.assertRaises(rc.ReelCheckError):
            rc.normalize(Args(video=self.v, output=self.v))

    def test_normalize_errore_ffmpeg_non_segnala_successo(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(rc.ReelCheckError):
            rc.normalize(Args(video=os.path.join(self.d, "non-esiste.mp4")))
        self.assertNotIn("Uscita:", buf.getvalue())
        bad = os.path.join(self.d, "rotto.mp4"); open(bad, "wb").write(b"not a video") if False else None; fh = open(bad, "wb"); fh.write(b"not a video"); fh.close()
        with redirect_stdout(buf), self.assertRaises(rc.ReelCheckError):
            rc.normalize(Args(video=bad))
        self.assertNotIn("Uscita:", buf.getvalue())

    def test_normalize_ok_e_ricontrollo(self):
        out = os.path.join(self.d, "clip-norm.mp4")
        buf = io.StringIO()
        with redirect_stdout(buf):
            _, before, after = rc.normalize(Args(video=self.v, output=out))
        self.assertTrue(os.path.exists(out))
        self.assertLessEqual(abs(after - rc.RULES["lufs_target"]), rc.RULES["lufs_tolleranza"])
        with self.assertRaises(rc.ReelCheckError):
            rc.normalize(Args(video=self.v, output=out))  # non sovrascrive senza --overwrite

    def test_cli_errore_esce_con_1(self):
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "reelcheck.py"), "normalize", os.path.join(self.d, "manca.mp4")], capture_output=True, text=True)
        self.assertEqual(p.returncode, 1); self.assertIn("ERRORE", p.stderr)


if __name__ == "__main__":
    unittest.main()
