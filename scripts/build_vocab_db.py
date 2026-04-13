"""
DeutschMeister — Kelime Veritabanı Kurulum Scripti
====================================================
Çalıştırma:
  cd /home/user/workspace/backend
  python scripts/build_vocab_db.py

Yaptığı işler:
  1. topics tablosunu CEFR/Goethe bağlam haritasına göre doldurur
  2. Goethe A1, A2, B1 PDF'lerini parse eder
  3. Her kelimeyi doğru topic'e eşler (kural tabanlı + anahtar kelime)
  4. words tablosuna yazar (tekrar çalıştırma güvenli — UPSERT)
  5. Özet istatistik basar
"""

import re
import sys
import json
import sqlite3
from pathlib import Path
from typing import Optional

import pdfplumber

# ─── Yollar ────────────────────────────────────────────────────────────────
BACKEND_DIR = Path(__file__).parent.parent
DB_PATH = BACKEND_DIR / "deutschmeister.db"
PDF_DIR = BACKEND_DIR / "data" / "vocab_pdfs"

# ─── BAĞLAM HARİTASI (araştırma bulgularından) ────────────────────────────
# Goethe Themenbereiche + CEFR domain araştırması + Türkçe öğrenci öncelikleri
TOPICS = [
    # slug, name_de, name_tr, description_tr, min_level, parent_slug
    # ── KİŞİSEL ──
    ("kisisel",       "Person & Identität",        "Kişisel Bilgiler",
     "İsim, adres, milliyet, yaş, medeni durum, kimlik",          "A1", None),

    ("aile",          "Familie & Beziehungen",     "Aile ve Akrabalar",
     "Aile üyeleri, akrabalar, ilişkiler, evlilik",               "A1", "kisisel"),

    ("dis_gorunus",   "Aussehen & Charakter",      "Görünüş ve Karakter",
     "Fiziksel özellikler, kişilik, duygular",                    "A2", "kisisel"),

    # ── EV & YAŞAM ──
    ("ev_yasami",     "Wohnen & Haushalt",         "Ev ve Yaşam Alanı",
     "Ev, daire, odalar, mobilya, kira, taşınma",                 "A1", None),

    ("gunluk_yasam",  "Alltag & Tagesablauf",      "Günlük Yaşam",
     "Günlük rutin, zaman, aktiviteler, alışkanlıklar",           "A1", None),

    # ── GIDA & ALIŞVERİŞ ──
    ("yiyecek_icecek","Essen & Trinken",            "Yiyecek ve İçecek",
     "Yemekler, içecekler, restoran, tarif, beslenme",            "A1", None),

    ("alisveris",     "Einkaufen & Konsum",         "Alışveriş",
     "Mağazalar, fiyat, ödeme, kıyafet, market",                  "A1", None),

    # ── SAĞLIK ──
    ("saglik",        "Gesundheit & Körper",        "Sağlık ve Vücut",
     "Vücut, hastalık, belirtiler, ilaç, doktor",                 "A2", None),

    ("hastane",       "Arztbesuch & Krankenhaus",   "Hastane ve Klinik",
     "Doktor ziyareti, muayene, reçete, acil servis, sigorta",    "A2", "saglik"),

    # ── İŞ HAYATI ──
    ("is_hayati",     "Arbeit & Beruf",             "İş Hayatı",
     "Meslekler, işyeri, maaş, iş başvurusu, çalışma koşulları", "A1", None),

    ("is_basvuru",    "Bewerbung & Karriere",        "İş Başvurusu",
     "CV, mülakat, staj, kariyer, özgeçmiş",                      "B1", "is_hayati"),

    # ── EĞİTİM ──
    ("egitim",        "Bildung & Schule",            "Eğitim ve Okul",
     "Okul, ders, öğretmen, notlar, üniversite, dil öğrenimi",    "A1", None),

    ("cocuk_egitim",  "Kinderbetreuung & Erziehung","Çocuk Bakımı ve Eğitimi",
     "Kreş, anaokulp, ebeveynlik, çocuk gelişimi",                "A2", "egitim"),

    # ── RESMİ İŞLER ──
    ("resmi_isler",   "Behörden & Ämter",           "Resmi Daireler",
     "Belediye, pasaport, vize, oturma izni, vergi dairesi, polis","B1", None),

    ("banka",         "Bank & Finanzen",             "Banka ve Finans",
     "Hesap, transfer, kredi kartı, kira ödemesi, sigorta",       "A1", "resmi_isler"),

    ("posta",         "Post & Telekommunikation",    "Posta ve İletişim",
     "Mektup, kargo, telefon, internet, e-posta",                 "A1", "resmi_isler"),

    # ── SEYAHAT ──
    ("seyahat",       "Reisen & Verkehr",            "Seyahat ve Ulaşım",
     "Ulaşım araçları, bilet, otel, yol tarifi, hava alanı",      "A1", None),

    ("yol_tarifi",    "Wegbeschreibung & Stadt",     "Yol Tarifi ve Şehir",
     "Sokak, yön, meydanlar, şehir içi ulaşım",                   "A1", "seyahat"),

    # ── SOSYAL YAŞAM ──
    ("sosyal_yasam",  "Soziales Leben & Freizeit",  "Sosyal Yaşam ve Boş Zaman",
     "Arkadaşlık, davetler, hobiler, eğlence, kültür",            "A1", None),

    ("spor",          "Sport & Aktivitäten",         "Spor ve Aktiviteler",
     "Spor dalları, fitness, açık hava etkinlikleri",              "A1", "sosyal_yasam"),

    ("medya",         "Medien & Unterhaltung",       "Medya ve Eğlence",
     "TV, radyo, internet, sosyal medya, kitap, film",             "A2", "sosyal_yasam"),

    # ── DOĞA & ÇEVRE ──
    ("doga",          "Natur & Umwelt",              "Doğa ve Çevre",
     "Hava durumu, mevsimler, hayvanlar, bitkiler, çevre",         "A1", None),

    # ── TOPLUM & POLİTİKA ──
    ("toplum",        "Gesellschaft & Politik",      "Toplum ve Politika",
     "Ülkeler, kültür, dil, göç, haklar, vatandaşlık",            "B1", None),

    ("entegrasyon",   "Integration & Migration",     "Entegrasyon ve Göç",
     "Almanya'ya göç, oturma izni, dil kursları, sosyal destek",  "B1", "toplum"),

    # ── SAYILAR / ZAMAN / TEMEL ──
    ("sayilar_zaman", "Zahlen, Zeit & Masse",        "Sayılar, Zaman ve Ölçüler",
     "Sayılar, saat, tarih, günler, aylar, mevsimler, ölçüler",   "A1", None),

    ("renkler_sekiller","Farben & Formen",           "Renkler ve Şekiller",
     "Renkler, şekiller, boyutlar, miktarlar",                     "A1", None),
]

# ─── KELİME → TOPIC EŞLEŞTİRME KURALLARI ────────────────────────────────
# Her kural: (kelime/prefix listesi, topic_slug)
# Önce tam eşleşme, sonra prefix, sonra fallback
KEYWORD_TOPIC_MAP = {
    # Kişisel
    "kisisel": [
        "Name", "Vorname", "Nachname", "Familienname", "Adresse", "Telefon",
        "Handy", "E-Mail", "Geburtstag", "Geburtsdatum", "Geburtsort",
        "Alter", "Nationalität", "Staatsangehörigkeit", "Geschlecht",
        "männlich", "weiblich", "ledig", "verheiratet", "geschieden",
        "Ausweis", "Pass", "Personalausweis", "Postleitzahl", "Vorwahl",
        "Unterschrift", "Formular", "Anmeldung", "vorstellen",
    ],
    "aile": [
        "Familie", "Mutter", "Vater", "Bruder", "Schwester", "Kind",
        "Sohn", "Tochter", "Eltern", "Großmutter", "Großvater",
        "Geschwister", "Verwandte", "Oma", "Opa", "Ehemann", "Ehefrau",
        "Partner", "Partnerin", "Onkel", "Tante", "Cousin", "Nichte",
        "Neffe", "heiraten", "Hochzeit", "Scheidung",
    ],
    "dis_gorunus": [
        "Haar", "Auge", "Nase", "Mund", "Gesicht", "Körper", "Arm",
        "Bein", "Hand", "groß", "klein", "schlank", "dick", "jung",
        "alt", "hübsch", "freundlich", "nett", "lustig", "ruhig",
        "Gefühl", "Stimmung", "Charakter",
    ],
    # Ev yaşamı
    "ev_yasami": [
        "Wohnung", "Haus", "Zimmer", "Küche", "Bad", "Schlafzimmer",
        "Wohnzimmer", "Möbel", "Tisch", "Stuhl", "Bett", "Schrank",
        "Sofa", "Kühlschrank", "Herd", "Miete", "Vermieter", "Keller",
        "Balkon", "Garten", "Aufzug", "Etage", "Stock", "Eingang",
        "wohnen", "mieten", "umziehen", "einrichten",
    ],
    "gunluk_yasam": [
        "Morgen", "Mittag", "Abend", "Nacht", "aufstehen", "schlafen",
        "frühstücken", "Frühstück", "Alltag", "Tagesablauf", "Routine",
        "Pause", "Termin", "pünktlich", "früh", "spät",
    ],
    # Yiyecek
    "yiyecek_icecek": [
        "Essen", "Trinken", "Brot", "Fleisch", "Fisch", "Gemüse", "Obst",
        "Apfel", "Banane", "Kartoffel", "Reis", "Nudel", "Salat", "Suppe",
        "Kuchen", "Milch", "Kaffee", "Tee", "Wasser", "Saft", "Bier",
        "Wein", "Mahlzeit", "Frühstück", "Mittagessen", "Abendessen",
        "Restaurant", "Café", "Lokal", "Speisekarte", "bestellen",
        "kochen", "backen", "Rezept", "Zutaten", "schmecken",
    ],
    "alisveris": [
        "kaufen", "verkaufen", "Geschäft", "Laden", "Markt", "Supermarkt",
        "Kasse", "Preis", "teuer", "billig", "günstig", "bezahlen",
        "Rechnung", "Karte", "bar", "Kleidu", "Jacke", "Hose", "Schuhe",
        "Hemd", "Kleid", "Mantel", "Größe", "Angebot", "Rabatt",
    ],
    # Sağlık
    "saglik": [
        "gesund", "krank", "Krankheit", "Fieber", "Husten", "Schmerz",
        "weh", "Kopf", "Bauch", "Rücken", "Medikament", "Tablette",
        "Apotheke", "Rezept", "Allergie", "Impfung", "Blut",
    ],
    "hastane": [
        "Arzt", "Ärztin", "Krankenhaus", "Klinik", "Praxis", "Notaufnahme",
        "Termin", "Untersuchung", "Operation", "Krankenversicherung",
        "Krankenkasse", "Behandlung", "Patient", "Pfleger", "Schwester",
    ],
    # İş
    "is_hayati": [
        "Arbeit", "Beruf", "Job", "Chef", "Kollege", "Arbeitsplatz",
        "Büro", "Fabrik", "Firma", "Unternehmen", "Gehalt", "Lohn",
        "arbeiten", "Urlaub", "Kündigung", "Teilzeit", "Vollzeit",
        "Schicht", "Überstunden", "Arbeitszeit",
    ],
    "is_basvuru": [
        "Bewerbung", "bewerben", "Lebenslauf", "Zeugnis", "Ausbildung",
        "Praktikum", "Stelle", "Stellenangebot", "Vorstellungsgespräch",
        "Qualifikation", "Erfahrung",
    ],
    # Eğitim
    "egitim": [
        "Schule", "Klasse", "Lehrer", "Schüler", "Unterricht", "Hausaufgabe",
        "Prüfung", "Note", "lernen", "studieren", "Universität", "Hochschule",
        "Kurs", "Sprachkurs", "Sprache", "Wörterbuch", "Buch",
    ],
    "cocuk_egitim": [
        "Kindergarten", "Kinderbetreuung", "Kita", "Kleinkind",
        "erziehen", "Erziehung",
    ],
    # Resmi
    "resmi_isler": [
        "Amt", "Behörde", "Antrag", "Formular", "Genehmigung", "Visum",
        "Aufenthalt", "Ausweis", "Polizei", "Rathaus", "Bürgeramt",
        "anmelden", "Meldung", "Steuern", "Finanzamt",
    ],
    "banka": [
        "Bank", "Konto", "überweisen", "Überweisung", "Geld", "Euro",
        "Cent", "Karte", "Kredit", "Schulden", "abheben", "einzahlen",
        "Kontonummer", "IBAN",
    ],
    "posta": [
        "Post", "Brief", "Paket", "Briefmarke", "Absender", "Empfänger",
        "Postleitzahl", "schicken", "Telefon", "Handy", "anrufen",
        "Internet", "E-Mail", "Computer", "WLAN",
    ],
    # Seyahat
    "seyahat": [
        "reisen", "Reise", "fahren", "fliegen", "Zug", "Bus", "Auto",
        "Flugzeug", "Flughafen", "Bahnhof", "Fahrkarte", "Ticket",
        "Hotel", "Unterkunft", "übernachten", "Gepäck", "Koffer",
        "Urlaub", "Ausflug",
    ],
    "yol_tarifi": [
        "Straße", "Weg", "links", "rechts", "geradeaus", "Kreuzung",
        "Haltestelle", "U-Bahn", "S-Bahn", "Straßenbahn", "Stadtplan",
        "Norden", "Süden", "Osten", "Westen",
    ],
    # Sosyal
    "sosyal_yasam": [
        "Freund", "Freundin", "Bekannte", "Party", "feiern", "einladen",
        "Einladung", "treffen", "Hobby", "Interesse", "Freizeit",
        "Verein", "Club", "tanzen", "singen",
    ],
    "spor": [
        "Sport", "spielen", "Fußball", "Tennis", "schwimmen", "laufen",
        "wandern", "Rad fahren", "Fitnessstudio", "Training",
    ],
    "medya": [
        "Fernsehen", "Radio", "Zeitung", "Zeitschrift", "Film", "Kino",
        "Musik", "Buch", "lesen", "Internet", "Smartphone", "Social",
    ],
    # Doğa
    "doga": [
        "Wetter", "Regen", "Sonne", "Wind", "Schnee", "warm", "kalt",
        "Temperatur", "Grad", "Frühling", "Sommer", "Herbst", "Winter",
        "Baum", "Blume", "Tier", "Hund", "Katze", "Vogel", "Meer", "Berg",
    ],
    # Toplum
    "toplum": [
        "Land", "Staat", "Politik", "Regierung", "Gesetz", "Recht",
        "Gesellschaft", "Kultur", "Religion", "Bürger",
    ],
    "entegrasyon": [
        "Integration", "Migration", "Flüchtling", "Asyl", "Integrationskurs",
        "Einbürgerung", "Aufenthaltserlaubnis",
    ],
    # Temel
    "sayilar_zaman": [
        "Uhr", "Stunde", "Minute", "Tag", "Woche", "Monat", "Jahr",
        "Datum", "Montag", "Dienstag", "Mittwoch", "Donnerstag",
        "Freitag", "Samstag", "Sonntag", "Januar", "Februar", "März",
        "April", "Mai", "Juni", "Juli", "August", "September",
        "Oktober", "November", "Dezember", "Meter", "Kilo", "Liter",
    ],
    "renkler_sekiller": [
        "schwarz", "weiß", "rot", "blau", "grün", "gelb", "grau",
        "braun", "orange", "rosa", "lila", "Farbe",
    ],
}

# Topic slug -> id eşlemesi (DB'den doldurulacak)
TOPIC_SLUG_TO_ID: dict[str, int] = {}


# ─── YARDIMCI FONKSİYONLAR ──────────────────────────────────────────────

def detect_topic(word: str) -> Optional[str]:
    """Kelimeye göre en iyi topic slug'ını döner."""
    for slug, keywords in KEYWORD_TOPIC_MAP.items():
        for kw in keywords:
            if word.lower().startswith(kw.lower()) or kw.lower() in word.lower():
                return slug
    return None


def detect_article(token: str) -> tuple[Optional[str], str]:
    """'der Hund' → ('der', 'Hund'), 'gehen' → (None, 'gehen')"""
    parts = token.strip().split()
    if len(parts) >= 2 and parts[0].lower() in ("der", "die", "das"):
        return parts[0].lower(), " ".join(parts[1:])
    return None, token.strip()


def detect_word_type(word: str, article: Optional[str]) -> str:
    if article:
        return "noun"
    # Basit fiil tespiti: küçük harf başlangıcı + fiil eki
    if word and word[0].islower():
        if any(word.endswith(s) for s in ("en", "ern", "eln")):
            return "verb"
        if any(word.endswith(s) for s in ("lich", "ig", "isch", "los", "sam")):
            return "adj"
    return "other"


def parse_goethe_pdf(pdf_path: Path, level: str) -> list[dict]:
    """
    Goethe PDF'inden kelimeleri çıkarır.
    Her kelime için: {word, article, plural, word_type, example_de, level}
    """
    entries = []
    seen = set()

    with pdfplumber.open(pdf_path) as pdf:
        full_text = ""
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text += text + "\n"

    # Alfabetik kelime listesi bölümünü bul
    # Goethe PDF'leri "Alphabetische Wortliste" veya "ALPHABETISCHER WORTSCHATZ" başlığını kullanır
    markers = [
        "Alphabetische Wortliste",
        "Alphabetischer Wortschatz",
        "ALPHABETISCHER WORTSCHATZ",
        "ALPHABETISCHE WORTLISTE",
    ]
    start_idx = -1
    for marker in markers:
        idx = full_text.find(marker)
        if idx != -1:
            start_idx = idx + len(marker)
            break

    if start_idx == -1:
        # PDF parse edemedik, tüm metni kullan
        word_text = full_text
    else:
        word_text = full_text[start_idx:]

    # Satır satır işle
    lines = word_text.split("\n")
    current_word_info = None
    current_example = []

    for line in lines:
        line = line.strip()
        if not line or len(line) < 2:
            continue

        # Sayfa numaraları / başlıkları atla
        if re.match(r'^\d+$', line):
            continue
        if "WORTLISTE" in line.upper() and len(line) < 30:
            continue
        if "GOETHE" in line and "ZERTIFIKAT" in line:
            continue

        # Artikel ile başlayan isimler: "der Hund, -e" formatı
        artikel_match = re.match(
            r'^(der|die|das)\s+([A-ZÄÖÜ][a-zA-ZÄÖÜäöüß\-/]+)(,?\s*[-–¨"]+[a-zA-ZÄÖÜäöüß,\s]*)?',
            line
        )
        if artikel_match:
            art = artikel_match.group(1)
            noun = artikel_match.group(2)
            plural_raw = artikel_match.group(3) or ""
            # çoğul temizle
            plural = re.sub(r'[,\s]+', '', plural_raw).strip("-–¨\"") or None

            key = f"{art}_{noun}".lower()
            if key not in seen:
                seen.add(key)
                tricky = (art == "das" and noun[0].isupper() and
                          any(noun.endswith(s) for s in ("chen", "lein", "ment", "tum")))
                entry = {
                    "word": noun,
                    "article": art,
                    "plural": plural,
                    "word_type": "noun",
                    "level": level,
                    "has_tricky_article": tricky,
                    "topic_slug": detect_topic(noun),
                    "example_de": None,
                }
                entries.append(entry)
                current_word_info = entry
            continue

        # Fiil: küçük harf başlangıcı veya mastar -en eki
        verb_match = re.match(
            r'^([a-zäöüß][a-zäöüßA-ZÄÖÜ\(\)]+(?:en|ern|eln))\b',
            line
        )
        if verb_match:
            verb = verb_match.group(1).strip("()")
            if verb not in seen and len(verb) > 2:
                seen.add(verb)
                entry = {
                    "word": verb,
                    "article": None,
                    "plural": None,
                    "word_type": "verb",
                    "level": level,
                    "has_tricky_article": False,
                    "topic_slug": detect_topic(verb),
                    "example_de": None,
                }
                entries.append(entry)
                current_word_info = entry
            continue

        # Büyük harfli sıfat/zarf
        adj_match = re.match(r'^([a-zäöüß][a-zäöüßA-ZÄÖÜ]+(?:lich|ig|isch|los|sam|voll))\b', line)
        if adj_match:
            adj = adj_match.group(1)
            if adj not in seen:
                seen.add(adj)
                entry = {
                    "word": adj,
                    "article": None,
                    "plural": None,
                    "word_type": "adj",
                    "level": level,
                    "has_tricky_article": False,
                    "topic_slug": detect_topic(adj),
                    "example_de": None,
                }
                entries.append(entry)
                current_word_info = entry
            continue

        # Örnek cümle (büyük harf başlar, nokta ile biter, fiil içerir)
        if (current_word_info and
                re.match(r'^[A-ZÄÖÜ]', line) and
                len(line) > 10 and
                current_word_info.get("example_de") is None):
            current_word_info["example_de"] = line

    print(f"  [{level}] {pdf_path.name}: {len(entries)} kelime parse edildi")
    return entries


def insert_topics(conn: sqlite3.Connection) -> None:
    """topics tablosunu doldurur."""
    cur = conn.cursor()
    for (slug, name_de, name_tr, desc_tr, min_level, parent_slug) in TOPICS:
        cur.execute("""
            INSERT OR IGNORE INTO topics
              (slug, name_de, name_tr, description_tr, min_level, parent_slug)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (slug, name_de, name_tr, desc_tr, min_level, parent_slug))

    conn.commit()
    # ID'leri yükle
    for row in cur.execute("SELECT id, slug FROM topics"):
        TOPIC_SLUG_TO_ID[row[1]] = row[0]
    print(f"  {len(TOPIC_SLUG_TO_ID)} topic yüklendi")


def create_tables(conn: sqlite3.Connection) -> None:
    """Eksik tabloları oluşturur (idempotent)."""
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS topics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            slug        TEXT UNIQUE NOT NULL,
            name_de     TEXT NOT NULL,
            name_tr     TEXT NOT NULL,
            description_tr TEXT,
            min_level   TEXT DEFAULT 'A1',
            parent_slug TEXT
        );

        CREATE TABLE IF NOT EXISTS words (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            word                TEXT NOT NULL,
            article             TEXT,
            plural              TEXT,
            word_type           TEXT DEFAULT 'noun',
            base_form           TEXT,
            translation_tr      TEXT,
            example_de          TEXT,
            example_tr          TEXT,
            level               TEXT NOT NULL,
            source              TEXT DEFAULT 'goethe',
            frequency_rank      INTEGER,
            topic_id            INTEGER REFERENCES topics(id),
            has_tricky_article  INTEGER DEFAULT 0,
            created_at          TEXT DEFAULT (datetime('now')),
            UNIQUE(word, level, source)
        );

        CREATE TABLE IF NOT EXISTS fsrs_cards (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id      TEXT NOT NULL REFERENCES profiles(id),
            word_id         INTEGER NOT NULL REFERENCES words(id),
            stability       REAL DEFAULT 0.0,
            difficulty      REAL DEFAULT 5.0,
            retrievability  REAL DEFAULT 1.0,
            state           TEXT DEFAULT 'new',
            due             TEXT DEFAULT (datetime('now')),
            reps            INTEGER DEFAULT 0,
            lapses          INTEGER DEFAULT 0,
            last_review     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(profile_id, word_id)
        );

        CREATE INDEX IF NOT EXISTS idx_words_level    ON words(level);
        CREATE INDEX IF NOT EXISTS idx_words_topic    ON words(topic_id);
        CREATE INDEX IF NOT EXISTS idx_words_type     ON words(word_type);
        CREATE INDEX IF NOT EXISTS idx_fsrs_profile   ON fsrs_cards(profile_id);
        CREATE INDEX IF NOT EXISTS idx_fsrs_due       ON fsrs_cards(due);
    """)
    conn.commit()
    print("  Tablolar oluşturuldu/doğrulandı")


def insert_words(conn: sqlite3.Connection, entries: list[dict]) -> int:
    """Kelimeleri DB'ye yazar, tekrar çalıştırma güvenli (IGNORE)."""
    cur = conn.cursor()
    inserted = 0
    for e in entries:
        topic_id = TOPIC_SLUG_TO_ID.get(e.get("topic_slug")) if e.get("topic_slug") else None
        cur.execute("""
            INSERT OR IGNORE INTO words
              (word, article, plural, word_type, example_de, level, source,
               topic_id, has_tricky_article)
            VALUES (?, ?, ?, ?, ?, ?, 'goethe', ?, ?)
        """, (
            e["word"],
            e.get("article"),
            e.get("plural"),
            e.get("word_type", "other"),
            e.get("example_de"),
            e["level"],
            topic_id,
            1 if e.get("has_tricky_article") else 0,
        ))
        if cur.rowcount:
            inserted += 1

    conn.commit()
    return inserted


def print_stats(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    print("\n" + "="*52)
    print("  DeutschMeister Kelime DB — İstatistikler")
    print("="*52)

    print("\n📚 Seviyeye göre kelime sayısı:")
    for row in cur.execute("SELECT level, COUNT(*) FROM words GROUP BY level ORDER BY level"):
        print(f"  {row[0]}: {row[1]} kelime")

    print("\n🏷️  Kelime türüne göre:")
    for row in cur.execute("SELECT word_type, COUNT(*) FROM words GROUP BY word_type ORDER BY 2 DESC"):
        print(f"  {row[0]}: {row[1]}")

    print("\n🗂️  Bağlama göre dağılım (Top 15):")
    for row in cur.execute("""
        SELECT t.name_tr, t.min_level, COUNT(w.id) as n
        FROM topics t
        LEFT JOIN words w ON w.topic_id = t.id
        GROUP BY t.id
        ORDER BY n DESC
        LIMIT 15
    """):
        bar = "█" * min(row[2] // 5, 20)
        print(f"  {row[1]} | {row[0][:25]:<25} {bar} ({row[2]})")

    total = cur.execute("SELECT COUNT(*) FROM words").fetchone()[0]
    topics_total = cur.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    no_topic = cur.execute("SELECT COUNT(*) FROM words WHERE topic_id IS NULL").fetchone()[0]
    print(f"\n✅ TOPLAM: {total} kelime, {topics_total} bağlam")
    print(f"   Bağlam atanamamış: {no_topic} kelime")
    print("="*52)


def main():
    print("\n🚀 DeutschMeister Kelime DB Kurulumu Başlıyor...\n")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print("1️⃣  Tablolar oluşturuluyor...")
    create_tables(conn)

    print("\n2️⃣  Bağlam (topic) kategorileri yazılıyor...")
    insert_topics(conn)

    pdf_configs = [
        (PDF_DIR / "goethe_a1.pdf", "A1"),
        (PDF_DIR / "goethe_a2.pdf", "A2"),
        (PDF_DIR / "goethe_b1.pdf", "B1"),
    ]

    total_inserted = 0
    print("\n3️⃣  PDF'ler parse ediliyor...")
    for pdf_path, level in pdf_configs:
        if not pdf_path.exists():
            print(f"  ⚠️  {pdf_path.name} bulunamadı, atlanıyor")
            continue
        print(f"\n  📄 {pdf_path.name} ({level})...")
        entries = parse_goethe_pdf(pdf_path, level)
        inserted = insert_words(conn, entries)
        total_inserted += inserted
        print(f"  ✅ {inserted} yeni kelime eklendi")

    print(f"\n4️⃣  İstatistikler hesaplanıyor...")
    print_stats(conn)

    conn.close()
    print("\n🎉 Tamamlandı!\n")


if __name__ == "__main__":
    main()
