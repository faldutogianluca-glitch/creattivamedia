# Reel Check di Creattiva

Controllo dei video brevi prima di pubblicarli. Misura il file, lo confronta con le regole editoriali di Creattiva e tiene uno storico per imparare dai risultati veri dei nostri post. Non inventa punteggi di viralità.

## Cosa misura
- Formato e durata
- Ritmo visivo: cambi di inquadratura o di grafica, movimento nei primi 3 secondi
- Audio: volume integrato (LUFS), pause e silenzi
- Parlato (con il file .srt): prima parola, parole al minuto, gancio dei primi 3 secondi
- Didascalia: chiamate all'azione, nota sull'uso dell'AI, hashtag

## Installazione
Serve Python 3.9 o successivo, con numpy e Pillow, e ffmpeg installato.

    pip install numpy pillow
    # macOS: brew install ffmpeg   ·   Windows: winget install ffmpeg

Facoltativo, per avere ganci e didascalie proposti da un modello che gira sul vostro computer: installate Ollama (ollama.com) e scaricate un modello, per esempio `ollama pull llama3.1`.

## Uso

    # analisi con report HTML e aggiornamento dello storico
    python3 reelcheck.py analyze video.mp4 --srt sottotitoli.srt --caption didascalia.txt

    # con le proposte del modello locale
    python3 reelcheck.py analyze video.mp4 --srt sottotitoli.srt --caption didascalia.txt --ollama-model llama3.1

    # volume a -14 LUFS (il video non viene ricodificato)
    python3 reelcheck.py normalize video.mp4

    # quando ci sono almeno 10 post con i risultati (CSV con colonne file, views, comments...)
    python3 reelcheck.py learn metriche.csv --metric views

## Regole
Le soglie sono in cima a `reelcheck.py` (`RULES`): sono scelte editoriali di Creattiva, non regole delle piattaforme. Si cambiano lì.

Codice scritto con l'ausilio dell'AI con la supervisione di Gianluca Falduto.
