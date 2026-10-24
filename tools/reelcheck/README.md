# Reel Check (versione 2)

Controllo tecnico dei video brevi di Creattiva prima della pubblicazione, con storico per imparare dai risultati reali. Non stima la "viralità" e non sostituisce le verifiche editoriali umane.

## Comandi

```
python3 reelcheck.py analyze VIDEO.mp4 --srt sottotitoli.srt --caption didascalia.txt --out report/ --history storico.csv [--version-tag v2]
python3 reelcheck.py normalize VIDEO.mp4 [--output VIDEO-14lufs.mp4] [--overwrite]
python3 reelcheck.py learn risultati.csv --history storico.csv --metric views [--platform linkedin] [--per-day] [--include-paid] [--min-n 10]
python3 -m unittest discover -s tests -v
```

Requisiti: Python 3.9+, numpy, ffmpeg e ffprobe nel PATH. Nessuna dipendenza da percorsi personali.

## Identificativi

| Identificativo | Cosa identifica | Come nasce |
| --- | --- | --- |
| `asset_id` | il file video (impronta SHA-256 del contenuto, 12 caratteri) | automatico; due file con lo stesso nome ma contenuto diverso hanno ID diversi |
| `versione` | una variante dello stesso soggetto (es. `v2-14lufs`) | `--version-tag`, facoltativo |
| `rilevazione_id` | una singola analisi | automatico, nuovo a ogni `analyze` |
| `piattaforma` + `post_id` | una pubblicazione dell'asset | nel file dei risultati, una riga per pubblicazione |
| `rilevato_il` | il momento in cui i risultati sono stati letti | nel file dei risultati |

Lo storico (`storico.csv`) registra ogni analisi; `learn` usa solo l'ultima analisi per asset, quindi rianalizzare lo stesso file non crea post fantasma.

## Cosa controlla `analyze`

Tre blocchi separati nel report HTML e nel JSON:

1. **Controlli tecnici (automatici)**: formato 9:16, durata 15–60 s, prima parola entro 1 s (solo dai sottotitoli: senza `.srt` il controllo resta "n.d." e il primo suono è riportato a parte, perché può essere musica o rumore), primo cambio visivo entro 3 s, un cambio ogni 5 s in media, nessun silenzio oltre 0,7 s, volume tra −18 e −10 LUFS, ritmo 130–190 parole/min, sottotitoli presenti, una sola chiamata all'azione (controllo lessicale: fallisce sia se la CTA manca sia se compaiono parole chiave diverse, per esempio CORSO e LOCALE insieme), nota sull'uso dell'AI.
2. **Verifiche editoriali (umane)**: fonti delle affermazioni, coerenza della CTA con l'offerta e con gli altri post del giorno, nessun dato inventato, corrispondenza volto/voce, approvazione di Gianluca. Restano "da confermare a mano".
3. **Risultati commerciali**: non si leggono all'analisi; arrivano dalle piattaforme e si confrontano con `learn`.

Le soglie sono regole editoriali di Creattiva (`RULES`), non regole delle piattaforme.

## `normalize`

Porta il volume a −14 LUFS con due passate di `loudnorm`, video copiato senza ricodifica. Rifiuta di scrivere sul file di ingresso, non sovrascrive un'uscita esistente senza `--overwrite`, scrive su un file temporaneo e lo rinomina solo a fine lavoro, e **rimisura il risultato**: se il valore misurato non è entro ±1,5 LUFS dall'obiettivo il comando esce con errore. Ogni errore di ffmpeg ferma il comando con codice di uscita 1 e nessun messaggio di successo.

## `learn`

File dei risultati, una riga per pubblicazione, con colonne obbligatorie `asset_id, piattaforma, post_id` e facoltative `pubblicato_il, rilevato_il, distribuzione (organica|pagata)` più le metriche (`views`, `impressions`, `comments`, `clicks`, …). Regole:

- più rilevazioni della stessa pubblicazione: si tiene l'ultima per `rilevato_il`;
- le pubblicazioni a pagamento sono escluse salvo `--include-paid`;
- `--per-day` divide la metrica per l'età del post in giorni (denominatore comparabile);
- piattaforme diverse insieme: il comando avverte che i denominatori non sono confrontabili e suggerisce `--platform`;
- correlazione di rango di Spearman con pari merito a rango medio; dati costanti o valori mancanti vengono scartati con un messaggio; sotto `--min-n` (10) nessuna conclusione.

L'output è **esplorativo**: con poche decine di post è un indizio su cosa guardare, non una causa e non una previsione.

## Test di regressione

`tests/test_reelcheck.py` copre i difetti trovati nella versione 1: risultati tutti uguali, analisi ripetute dello stesso file, piattaforme diverse con lo stesso nome file, CTA assente che passava il controllo, primo suono scambiato per prima parola, successo comunicato dopo un errore di ffmpeg, stesso percorso in ingresso e uscita, ranghi a pari merito, campione insufficiente, ricontrollo dopo la normalizzazione.

## Storia

- 2.0.0 (24/10/2026): identificativi distinti, controlli separati in tre blocchi, `normalize` sicuro con ricontrollo, `learn` per pubblicazione con Spearman corretto, test. La versione 1 è conservata in `reelcheck.v1.py.orig` per confronto.
- 1.0 (15/10/2026): prima versione.
