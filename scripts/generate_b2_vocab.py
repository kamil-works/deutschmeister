"""
DeutschMeister — B2 Kelime Listesi Üretici
==========================================
B2 için resmi Goethe PDF yok. Bu script:
1. Gemini'ye CEFR B2 bağlamlarına göre kelime ürettirir (JSON)
2. Üretilen kelimeleri doğrular ve DB'ye yazar
3. Her batch'i ayrı dosyaya kaydeder (hata toleransı)

Çalıştırma:
  cd /home/user/workspace/backend
  GEMINI_API_KEY=... python scripts/generate_b2_vocab.py
"""

import json, os, sqlite3, time
from pathlib import Path
from google import genai
from google.genai import types

API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyBIxnMpzVEuvuiNdXVxHMGjqVLTH4u-faA")
DB_PATH = Path(__file__).parent.parent / "deutschmeister.db"
CACHE_DIR = Path(__file__).parent.parent / "data" / "b2_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=API_KEY)

# B2 bağlamları ve her biri için hedef kelime sayısı
# Kaynak: CEFR B2 descriptor + Goethe B2 exam topics + iş/akademik Almanca araştırması
B2_TOPICS = [
    # (topic_slug, konu_aciklamasi, hedef_kelime_sayisi)
    ("is_hayati",     "Berufsleben: Bewerbung, Arbeitsmarkt, Berufsausbildung, Karriere, Arbeitsrecht, Tarifvertrag, Gewerkschaft, Arbeitslosigkeit, Kurzarbeit, Homeoffice", 40),
    ("toplum",        "Gesellschaft & Politik: Demokratie, Wahl, Parlament, Partei, Integration, Einwanderung, Bürgerrechte, soziale Gerechtigkeit, Armut, Wohlstand", 35),
    ("egitim",        "Bildung & Wissenschaft: Hochschule, Studium, Bachelor, Master, Forschung, Stipendium, Bildungssystem, Erwachsenenbildung, Weiterbildung, Digitalisierung", 35),
    ("saglik",        "Gesundheit & Medizin: Prävention, chronische Krankheit, psychische Gesundheit, Gesundheitssystem, Krankenkasse, Pflegeversicherung, Rehabilitation, Therapie", 30),
    ("resmi_isler",   "Behörden & Verwaltung: Bürgeramt, Standesamt, Ausländerbehörde, Sozialamt, Rentenbescheid, Steuererklärung, Kindergeld, Wohngeld, BAföG, Elterngeld", 35),
    ("doga",          "Umwelt & Nachhaltigkeit: Klimawandel, erneuerbare Energie, Recycling, CO2-Emissionen, Naturschutz, Biodiversität, Elektromobilität, Solarenergie", 25),
    ("medya",         "Medien & Kommunikation: Journalismus, Pressefreiheit, soziale Medien, Algorithmus, Datenschutz, Fake News, Influencer, Streaming, Podcast", 25),
    ("toplum",        "Wirtschaft & Finanzen: Inflation, Zinsen, Investition, Aktienmarkt, Haushalt, Subvention, Import/Export, Globalisierung, Fachkräftemangel", 30),
    ("sosyal_yasam",  "Kultur & Gesellschaft: Tradition, Multikulturalismus, Generationskonflikt, Werte, Ehrenamt, Gemeinschaft, Vereinsleben, Solidarität", 25),
    ("entegrasyon",   "Migration & Integration: Asylverfahren, Aufenthaltstitel, Einbürgerung, Anerkennung Abschlüsse, Sprachförderung, Diskriminierung, Chancengleichheit", 30),
    ("is_basvuru",    "Berufliche Kommunikation: Präsentation, Verhandlung, Protokoll, Geschäftsbrief, Kündigung, Abmahnung, Betriebsrat, Datenschutz am Arbeitsplatz", 25),
    ("egitim",        "Technologie & Digitalisierung: Künstliche Intelligenz, Algorithmus, Datenschutz, Cloud, App-Entwicklung, Automatisierung, Robotik, digitale Transformation", 25),
]

SYSTEM_PROMPT = """Du bist ein Experte für Deutsch als Fremdsprache (DaF) auf B2-Niveau.
Generiere eine strukturierte Vokabelliste im JSON-Format.

Anforderungen:
- Nur echte B2-Vokabeln (nicht A1-B1 bereits bekannte Wörter)
- Jede Vokabel mit Artikel (bei Nomen), Pluralform, Wortart und türkischer Übersetzung
- Keine Duplikate, keine zu einfachen Wörter
- Reale, prüfungsrelevante Vokabeln für das Thema

Ausgabe-Format (streng JSON, kein Markdown):
{
  "words": [
    {
      "word": "die Bewerbung",
      "article": "die",
      "base_word": "Bewerbung",
      "plural": "Bewerbungen",
      "word_type": "noun",
      "translation_tr": "iş başvurusu",
      "example_de": "Ich habe meine Bewerbung abgeschickt."
    }
  ]
}

Für Verben:
{
  "word": "beantragen",
  "article": null,
  "base_word": "beantragen",
  "plural": null,
  "word_type": "verb",
  "translation_tr": "başvurmak, talep etmek",
  "example_de": "Sie hat einen Aufenthaltstitel beantragt."
}
"""

def generate_batch(topic_slug: str, topic_desc: str, count: int, batch_num: int) -> list[dict]:
    cache_file = CACHE_DIR / f"{topic_slug}_{batch_num}.json"
    if cache_file.exists():
        print(f"    [cache] {cache_file.name}")
        with open(cache_file) as f:
            return json.load(f)

    prompt = f"""Thema: {topic_desc}
Generiere genau {count} B2-Vokabeln zu diesem Thema.
Wichtig: Nur Wörter die ein B2-Lerner noch nicht kennt (keine A1-B1 Grundvokabeln).
Antworte NUR mit dem JSON-Objekt, kein Text davor oder danach."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                response_mime_type="application/json",
            )
        )
        raw = response.text.strip()
        # JSON bloğu temizle
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        words = data.get("words", [])

        with open(cache_file, "w") as f:
            json.dump(words, f, ensure_ascii=False, indent=2)

        print(f"    [api] {len(words)} kelime uretildi -> {cache_file.name}")
        return words

    except Exception as e:
        print(f"    [hata] {e}")
        return []


def validate_and_insert(conn: sqlite3.Connection, words: list[dict],
                         topic_slug: str) -> tuple[int, int]:
    cur = conn.cursor()
    topic_row = cur.execute("SELECT id FROM topics WHERE slug=?", (topic_slug,)).fetchone()
    topic_id = topic_row[0] if topic_row else None

    inserted = 0
    skipped = 0
    valid_articles = {"der", "die", "das", None}
    valid_types = {"noun", "verb", "adj", "adv", "phrase", "other"}

    # B1'de olan kelimeleri al — B2'de duplikat olmasın
    existing = {r[0].lower() for r in cur.execute("SELECT word FROM words")}

    for w in words:
        word = w.get("base_word") or w.get("word", "")
        # "der Hund" formatından temizle
        if " " in word and word.split()[0].lower() in ("der", "die", "das"):
            word = " ".join(word.split()[1:])
        word = word.strip()
        if not word or len(word) < 2:
            skipped += 1
            continue

        article = w.get("article")
        if isinstance(article, str):
            article = article.lower().strip()
            if article not in ("der", "die", "das"):
                article = None

        word_type = w.get("word_type", "noun")
        if word_type not in valid_types:
            word_type = "other"

        translation = w.get("translation_tr", "")
        example = w.get("example_de", "")
        plural = w.get("plural")

        # Çok basit kelime filtresi — zaten A1-B1'de varsa atla
        if word.lower() in existing:
            skipped += 1
            continue

        try:
            cur.execute("""
                INSERT OR IGNORE INTO words
                  (word, article, plural, word_type, translation_tr, example_de,
                   level, source, topic_id, has_tricky_article)
                VALUES (?, ?, ?, ?, ?, ?, 'B2', 'gemini', ?, 0)
            """, (word, article, plural, word_type, translation, example, topic_id))
            if cur.rowcount:
                inserted += 1
                existing.add(word.lower())
            else:
                skipped += 1
        except Exception as e:
            skipped += 1

    conn.commit()
    return inserted, skipped


def main():
    print("\n🤖 B2 Kelime Listesi Üretimi Başlıyor...\n")
    conn = sqlite3.connect(DB_PATH)

    total_inserted = 0
    total_skipped = 0
    total_generated = 0

    for i, (topic_slug, topic_desc, count) in enumerate(B2_TOPICS, 1):
        print(f"\n[{i}/{len(B2_TOPICS)}] {topic_slug}")
        print(f"  Konu: {topic_desc[:60]}...")

        words = generate_batch(topic_slug, topic_desc, count, i)
        total_generated += len(words)

        inserted, skipped = validate_and_insert(conn, words, topic_slug)
        total_inserted += inserted
        total_skipped += skipped
        print(f"  DB: +{inserted} kelime ({skipped} atlandi)")

        time.sleep(0.5)  # rate limit

    conn.close()

    print("\n" + "="*50)
    print(f"  Uretilen: {total_generated}")
    print(f"  Eklenen:  {total_inserted}")
    print(f"  Atlanan:  {total_skipped}")
    print("="*50)

    # Test et
    conn2 = sqlite3.connect(DB_PATH)
    b2_count = conn2.execute("SELECT COUNT(*) FROM words WHERE level='B2'").fetchone()[0]
    print(f"\n  B2 toplam kelime: {b2_count}")
    print("\n  Ornek B2 kelimeler:")
    for r in conn2.execute("""
        SELECT w.article, w.word, w.translation_tr, t.name_tr
        FROM words w LEFT JOIN topics t ON w.topic_id=t.id
        WHERE w.level='B2'
        ORDER BY RANDOM() LIMIT 8
    """):
        print(f"    {r[0] or ''} {r[1]} = {r[2] or '?'} [{r[3] or '?'}]")
    conn2.close()
    print("\n✅ Tamamlandi!\n")


if __name__ == "__main__":
    main()
