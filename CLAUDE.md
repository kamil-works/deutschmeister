# DeutschMeister — CLAUDE.md

## Proje Özeti

Türkçe konuşan aileler için AI destekli Almanca öğretmeni. Slack üzerinden çalışan bir chatbot; Gemini API ile sesli konuşma pratiği, FSRS tabanlı kelime tekrarı ve günlük hatırlatmalar sunar.

**Deployment:** Railway (Docker) — `https://deutschmeister-production-b04a.up.railway.app`  
**GitHub:** `kamil-works/deutschmeister`

---

## Teknoloji Yığını

| Katman | Teknoloji |
|---|---|
| Web Framework | FastAPI + Uvicorn |
| AI / LLM | Google Gemini 2.5 Flash (text), Gemini 2.0 Flash Exp (Live/ses) |
| Spaced Repetition | FSRS (py-fsrs, %90 retention hedefi) |
| Veritabanı | SQLite async (aiosqlite + SQLAlchemy 2.0) |
| Migrasyon | Alembic |
| Slack | Slack Events API + Block Kit (httpx ile manuel HTTP) |
| Container | Docker multi-stage (python:3.12-slim) |
| Loglama | structlog (JSON) |
| Test | pytest + pytest-asyncio |

---

## Klasör Yapısı

```
app/
  main.py                  # FastAPI app fabrikası, startup/shutdown
  core/
    config.py              # Pydantic Settings (env vars)
    database.py            # AsyncSessionLocal, init_db()
  models/
    db.py                  # SQLAlchemy ORM modelleri
  schemas/
    structured_outputs.py  # Pydantic şemaları (Gemini çıktıları)
  api/routes/
    slack.py               # Slack Events/Actions/Commands webhook'ları
    chat.py                # Text chat endpoint + _chat_logic()
    exercises.py           # Egzersiz üretimi
    profiles.py            # Profil CRUD
    pronunciation.py       # Telaffuz değerlendirme
    voice.py               # Sesli seans WebSocket
    vocabulary.py          # Kelime yönetimi
  services/
    gemini_service.py      # Gemini REST (egzersiz üretimi)
    gemini_live_proxy.py   # Gemini Live API WebSocket proxy (ses)
    gemini_pronunciation.py# Telaffuz puanlama
    fsrs_engine.py         # Spaced Repetition motoru
    curriculum_engine.py   # Müfredat ve ders planı
    session_analyzer.py    # Seans sonrası AI analizi
    interleaved_scheduler.py # Karma ders zamanlayıcı
    daily_reminder.py      # Günlük Slack hatırlatma döngüsü
  agent/prompts/           # Sistem prompt'ları (.txt)
  static/
    session.html           # Sesli ders UI (tarayıcıda çalışır)
    audio-processor.js     # AudioWorklet (mikrofon → Gemini)
alembic/                   # DB migrasyon dosyaları
scripts/                   # Yardımcı script'ler (vocab seed, test)
```

---

## Ortam Değişkenleri (Railway)

```
GEMINI_API_KEY          Google AI Studio API anahtarı
SLACK_BOT_TOKEN         xoxb-... (Bot User OAuth Token)
SLACK_SIGNING_SECRET    Slack App → Basic Information'dan
PUBLIC_BASE_URL         https://deutschmeister-production-b04a.up.railway.app
DATABASE_URL            sqlite+aiosqlite:////app/data/deutschmeister.db
CORS_ORIGINS            https://deutschmeister-production-b04a.up.railway.app
SECRET_KEY              Rastgele güçlü şifre
```

---

## Veritabanı Şeması (Önemli Tablolar)

### profiles
- `id` UUID PK
- `name`, `age`, `level` (A1/A2/B1/B2)
- `slack_user_id` UNIQUE — Slack ↔ profil eşlemesi
- `slack_channel_id` — DM channel ID
- `reminder_state` — `"waiting_for_time"` | NULL
- `reminder_snoozed_until` — `"HH:MM"` UTC formatı
- `agent_strategy` JSON — AI Beyin Layer 2 (SessionAnalyzer günceller)
- `weekly_grammar_target` — Bu haftanın gramer hedefi

### sessions
- `profile_id` FK → profiles
- `mode` — `"conversation"` | `"pronunciation"`
- `plan_json` — SessionPlan sticky planı

### chat_messages
- `profile_id` FK, `role` (user/model), `content`
- Chat geçmişinin tamamı saklanır

### daily_logs
- `log_date` "YYYY-MM-DD", `session_count`, `total_duration_s`
- `words_learned`, `words_struggled`, `words_mastered`
- `session_quality` 0.0–1.0, `anxiety_signal` low/medium/high
- `ai_impressions` Türkçe AI özet, `error_patterns` JSON

---

## Slack Entegrasyonu

**Mimarisi:** `app/api/routes/slack.py` saf UI katmanıdır — iş mantığı içermez.

### Endpoint'ler
| Endpoint | Amaç |
|---|---|
| `POST /slack/events` | Events API webhook (url_verification + message) |
| `POST /slack/actions` | Block Kit buton etkileşimleri |
| `POST /slack/commands` | `/ders` slash komutu |

### Slack'ten Gelen Mesaj Akışı
1. Kullanıcı Slack'e mesaj yazar
2. `/slack/events` → imza doğrulama (HMAC-SHA256)
3. `profil:` prefix'i → `_handle_profile_create()`
4. `ders başlat` → `_handle_voice_command()` → session oluştur → link gönder
5. Diğer mesajlar → `_get_profile_id()` → `_chat_logic()` → Slack'e yaz

### Profil Oluşturma Formatı (Slack'ten)
```
profil: Kamil, 34, A1
```
→ Profile kaydı + FSRS kartları otomatik oluşturulur.

### URL Verification (Challenge) Davranışı
`SLACK_SIGNING_SECRET` boşsa imza doğrulama atlanır (geliştirme ortamı güvenliği). Prodüksiyonda mutlaka set edilmeli.

---

## AI Mimarisi (3 Katman)

### Layer 0 — FSRS Kelime Motoru
- `fsrs_engine.py` → `FSRSEngine` sınıfı
- %90 retention hedefi, `Scheduler(desired_retention=0.90)`
- Rating: 1=Again, 2=Hard, 3=Good, 4=Easy
- `initialize_cards()` → profil oluşturulunca otomatik çalışır
- `get_due_cards()` → bugün tekrar edilecek kartlar

### Layer 1 — Constitution
- `weekly_grammar_target` DB field'ı
- `agent/prompts/tutor_system.txt` ana sistem prompt'u
- Admin veya sistem tarafından yazılır

### Layer 2 — Agent Expression Engine
- `session_analyzer.py` → seans sonrası `agent_strategy` JSON'unu günceller
- `agent_strategy`: kullanıcının güçlü/zayıf yönleri, kaygı seviyesi, önerilen strateji
- Her chat'te bu JSON sistem prompt'una enjekte edilir

---

## Sesli Ders Akışı

1. Kullanıcı `ders başlat` yazar → `_handle_voice_command()`
2. Session DB'ye kaydedilir → unique `session_id`
3. `PUBLIC_BASE_URL/session/{session_id}` linki Slack'e gönderilir
4. Kullanıcı tarayıcıda açar → `static/session.html`
5. `audio-processor.js` AudioWorklet mikrofonu yakalar
6. `voice.py` WebSocket → `gemini_live_proxy.py` → Gemini Live API

---

## Günlük Hatırlatma Sistemi

- `daily_reminder.py` → `reminder_loop()` startup'ta `asyncio.create_task()` ile başlar
- Her dakika DB'yi tarar: `last_reminder_date != bugün` olan profilleri bulur
- `reminder_snoozed_until` set edilmişse o saatten önce mesaj göndermez
- Türkiye saati (UTC+3) baz alınır

---

## Geliştirme Rehberi

### Lokal Kurulum
```bash
cp .env.example .env  # değerleri doldur
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

### Docker
```bash
docker compose up --build
```

### Test
```bash
pytest tests/ -v
```

### Yeni Migrasyon
```bash
alembic revision --autogenerate -m "açıklama"
alembic upgrade head
```

---

## Önemli Kurallar

1. **`slack.py` iş mantığı içermez** — sadece Slack ↔ backend köprüsü
2. **Tüm ID'ler UUID string**, tarihler UTC datetime
3. **DB path:** Docker/Railway'de `/app/data/deutschmeister.db` — volume olmadan her deploy'da sıfırlanır
4. **FSRS kartları** profil oluşturulunca otomatik seed'lenir (`initialize_cards`)
5. **Gemini Live API** sadece WebSocket üzerinden; REST endpoint'lerde `gemini_service.py` kullanılır
6. **`_chat_logic()`** `chat.py`'de — Slack ve HTTP endpoint aynı fonksiyonu çağırır
