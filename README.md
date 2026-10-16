# LLM-Inferenz – kleiner Prototyp

Qwen3-8B über vLLM, ohne LiteLLM. Dieses Repository enthält ausschließlich den
Inferenzdienst und dessen Build/Tests. UI, Accounts, Preise, Zahlung und
Kubernetes-Deployment gehören in andere Repositories.

## Die Dateien

| Datei | Aufgabe |
| --- | --- |
| `api.py` | Kleine Chat-API, Streaming und Verbrauchszähler. |
| `run.py` | Startet und überwacht vLLM mit fester Konfiguration. |
| `model.py` | Lädt beim Build exakt die erlaubten Modelldateien; prüft sie erneut beim Start. |
| `model.lock.json` | **Einzige Referenzdatei:** Modell-Commit, Größe und SHA-256 jeder Gewichts-, Konfigurations- und Tokenizer-Datei. |
| `Dockerfile` | vLLM-Basis per Digest, Modellgewichte im Image, kein Nachladen zur Laufzeit. |
| `tests/` | API-Grenzen, getrennte Anfragen, Streaming/Verbrauch und Modellintegrität. |
| `.github/workflows/` | Automatische Tests sowie manuell oder per Tag gestarteter GHCR-Build. |

## Festgelegtes Modell

- [Qwen/Qwen3-8B](https://huggingface.co/Qwen/Qwen3-8B), Apache-2.0.
- [Modellstand b968826…](https://huggingface.co/Qwen/Qwen3-8B/tree/b968826d9c46dd6066d109eabc6255188de91218).
- BF16, etwa 16,4 GB Modelldateien; zusätzlich benötigen vLLM und KV-Cache GPU-Speicher.
- vLLM 0.31.0, offizielles Image **mit festem Digest** im Dockerfile; CUDA 13.0.2.
- 8192 Kontexttokens, höchstens 1024 neue Tokens, zwei gleichzeitige Anfragen.
- Thinking ausgeschaltet. Keine Tools, LoRA-Adapter oder vom Client gewählte Templates/Modellpfade.

## Starten

NVIDIA-GPU mit passendem Treiber und Container Toolkit vorausgesetzt. Zum Beispiel
eine Karte mit 32 GB VRAM; die tatsächliche Speicherbelegung muss beim GPU-Test geprüft werden.

```sh
docker build -t llm-confidential .
# Zwei verschiedene, zufällige Schlüssel mit mindestens 32 Zeichen setzen:
export API_KEY="$(openssl rand -hex 32)"
export USAGE_API_KEY="$(openssl rand -hex 32)"
docker run --rm --gpus '"device=0"' --shm-size=2g \
  --read-only --tmpfs /tmp:rw,nosuid,size=8g \
  --cap-drop=ALL --security-opt=no-new-privileges \
  -p 127.0.0.1:8080:8080 -e API_KEY -e USAGE_API_KEY llm-confidential
```

Der erste Build lädt die gepinnten Gewichte herunter. Der Start prüft alle
Dateien, bevor vLLM sie lädt. vLLM selbst lauscht nur auf `127.0.0.1:8001`
im Container. Nach außen geht ausschließlich die kleine API auf Port 8080.
API-Zugriff gehört hinter einen vertrauenswürdigen Account-Dienst; Service-Schlüssel
gehören nicht ins Browser-Frontend. TLS und Netzwerkregeln sind Aufgabe des Deployments.

```sh
curl -i http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b","messages":[{"role":"user","content":"Sag kurz Hallo."}],"stream":true,"max_tokens":64}'
```

`stream:false` liefert eine normale JSON-Antwort. `stream:true` liefert SSE mit
Tokenzählern und abschließend `[DONE]`. Unbekannte Optionen werden abgelehnt.

## Verbrauch und spätere Abrechnung

Jede angenommene Anfrage erhält eine neue ID im Header `X-Request-ID`.
Der Account-Dienst ordnet diese ID seinem authentifizierten Nutzer zu.
Nach Abschluss kann er die Zähler separat abrufen:

```sh
curl http://127.0.0.1:8080/internal/usage/REQUEST_ID \
  -H "Authorization: Bearer $USAGE_API_KEY"
```

Die Antwort enthält Modell, Zeitpunkte, Status und `prompt_tokens`,
`completion_tokens`, `total_tokens`. Gezählt werden vLLM-Token-IDs einschließlich
Chat-Template, keine Zeichen oder Wörter. Sie enthält keine Inhalte oder Nutzer-ID.
Chat-Schlüssel dürfen diesen Endpunkt nicht lesen; Verbrauchsschlüssel dürfen
keine Chats starten. Rohes vLLM einschließlich Verwaltungsrouten wird nicht weitergereicht.

`usage_complete:true` setzt einen regulären Abschluss und finale Zähler voraus.
Bei Fehler/Abbruch sind Zähler gegebenenfalls nur teilweise verfügbar oder `null`.
Der Upstream wird beim Abbruch geschlossen; verbleibende GPU-Arbeit lässt sich damit
nicht exakt nachträglich abrechnen. Diese Fälle müssen außerhalb gesondert behandelt werden.

**Prototyp:** maximal die letzten 1000 abgeschlossenen Anfragen im Arbeitsspeicher;
Neustarts löschen sie. Der Account-Dienst muss sie zeitnah und idempotent anhand der ID
speichern. Es gibt noch kein dauerhaftes oder signiertes Abrechnungsjournal.

## Was „sauberer Zustand“ hier bedeutet

Jede Anfrage enthält ihre eigenen Nachrichten. Der Dienst speichert keine
Chat-Historie, lernt keine neuen Gewichte und verwendet keinen gemeinsamen
Prefix-Cache. vLLM verwaltet die KV-Zustände separat pro Anfrage. Ein erneuter
Pod-Start pro Nutzer ist dafür nicht vorgesehen.

Das ist logische Trennung, kein Beweis gelöschter GPU-Speicherzellen und keine
Isolation gegen alle Seitenkanäle. Gemeinsame Hardware kann Laufzeit und durch
Batching auch numerische Ergebnisse beeinflussen. Der Client muss seine Nachrichten
selbst korrekt trennen. Prompt-/Antwort- und HTTP-Zugriffslogs sind ausgeschaltet.

## Vertrauensgrenze

Dieses Image ist zunächst ein **gewöhnlicher GPU-Worker**. Die Dateiprüfung ist
eine lokale Integritätsprüfung, keine Hardware-Attestation. Ein böswilliger Host
könnte den Worker verändern. Deshalb sind Verbrauchsdaten ausdrücklich `attested:false`.

CPU-TEE, Bindung des Sitzungsschlüssels an den Report, geschützter Stream bis zur
GPU und GPU-Attestation sind hier **noch nicht integriert**. Der vertrauliche
Whisper-Client kann dieses Protokoll nicht unverändert verwenden. Ein öffentlicher
LLM-Endpunkt wird von diesem Repository nicht eingerichtet. NVIDIA-CC wird nicht vorgetäuscht.

## Prüfen und Image bauen

```sh
python -m pip install fastapi==0.138.0 httpx==0.28.1 uvicorn==0.35.0 pydantic==2.13.4
python -m unittest discover -s tests -v
```

Die API-Tests simulieren vLLM; sie belegen keinen GPU-Lauf. Die Pipeline `Image`
prüft zusätzlich die API im tatsächlichen Basisimage. Sie baut mit den festen
Gewichten und veröffentlicht nach GHCR unter `sha-<Git-Commit>`; der vollständige
Image-Digest steht in der Workflow-Zusammenfassung. Es gibt noch kein Release-/Versionsregister.
GHCR-Pakete müssen in GitHub separat öffentlich freigegeben werden; ein öffentliches
Repository macht ein neues Paket nicht automatisch öffentlich.


## CPU-TEE-Gateway für den Chat

`tee.py` läuft separat in einer AMD-SNP-VM; der GPU-Worker bleibt unverändert.
`Dockerfile.tee` übernimmt die per Digest festgelegte Whisper-CPU-Basis mit ihren
gesperrten Abhängigkeiten und kopiert nur Gateway und SNP-ABI-Encoder `evidence.py` hinein.

`/llm-api/attestation` liefert ausschließlich AMD-Evidence und die gebundenen
öffentlichen Sitzungsdaten. Der Client prüft AMD-Kette, Challenge, Gastmessung und
HOST_DATA. Anschließend lädt er `verification/accepted.json` und die drei Dateien
des passenden Eintrags direkt von GitHub, jeweils aus demselben aufgelösten Commit.
Er hasht InitData und Policy und prüft den darin festgelegten CPU-Image-Digest.
Ohne erfolgreiche Prüfung sendet er keine Nachrichten. Die Gastmessung wird wie
bei Whisper administrativ referenziert, nicht unabhängig nachgebaut. Maintainer
dieses Repositories und der vertrauenswürdig bezogene Client bleiben Vertrauensannahmen.

`/llm-api/chat` akzeptiert ausschließlich AES-GCM-verschlüsselte Anfragen. ECDH P-256
und HKDF-SHA256 verwenden die eigene Domäne `llm-cpu-tee-v1`. Jeder verschlüsselte
Antwort-Frame bindet Sitzung, Anfragenummer und Reihenfolge. Veränderte Frames und
fehlende Endmarkierungen führen zu einem Fehler, ohne Klartext-Fallback. Sitzungen
laufen nach 30 Minuten ab; alte Anfragen werden abgewiesen. Die gebundene Policy
sperrt Exec und Policywechsel. UI und Kubernetes-Deployment liegen weiterhin außerhalb.

**Grenze:** Hinter der CPU-TEE gehen Nachrichten per HTTP zum gewöhnlichen
GPU-Worker. GPU, Modell, deren Antworten und Tokenzähler sind nicht
hardware-attestiert. Der externe `GPU_API_KEY` darf als einziger variabler
Anwendungsparameter genau 64 hexadezimale Zeichen enthalten. Er ist ein
Zugangsschlüssel zum ungeschützten Worker und kein Vertrauensanker; sein Wert steht
nicht in den öffentlichen Prüfdateien. Zieladresse und Programmstart stehen im Code.

Die Hinweise auf fehlende TEE im vorherigen Abschnitt betreffen das GPU-Image.
Der zusätzliche CPU-Gateway schützt den Kanal bis zur CPU.
Protokolltests: `python -m unittest discover -s tee-tests -v` (simulierte Hardware).
Die Pipeline baut GPU- und CPU-Image getrennt.
