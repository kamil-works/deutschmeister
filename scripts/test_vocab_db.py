"""
DeutschMeister — Vocabulary DB Doğruluk Testleri
Çalıştırma: cd /home/user/workspace/backend && python scripts/test_vocab_db.py
"""
import sqlite3, sys
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "deutschmeister.db"
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

PASS = "✅"
FAIL = "❌"
errors = []

def check(label, condition, detail=""):
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label} {detail}")
        errors.append(label)

print("\n" + "="*60)
print("  DeutschMeister Vocabulary DB — Doğruluk Testleri")
print("="*60)

# ── 1. Tablo varlığı ──────────────────────────────────────────
print("\n[1] Tablo Varlığı")
tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")}
for t in ["topics", "words", "fsrs_cards", "profiles"]:
    check(f"Tablo '{t}' mevcut", t in tables)

# ── 2. Sayısal doğruluk ───────────────────────────────────────
print("\n[2] Kelime Sayıları (Goethe standardı)")
counts = {r[0]: r[1] for r in cur.execute("SELECT level, COUNT(*) FROM words GROUP BY level")}
a1_count = counts.get("A1", 0)
a2_count = counts.get("A2", 0)
b1_count = counts.get("B1", 0)
b2_count = counts.get("B2", 0)
total = sum(counts.values())

check(f"A1 >= 500 kelime (su an: {a1_count})", a1_count >= 500)
check(f"A2 >= 700 kelime (su an: {a2_count})", a2_count >= 700)
check(f"B1 >= 1500 kelime (su an: {b1_count})", b1_count >= 1500)
check(f"Toplam >= 2700 kelime (su an: {total})", total >= 2700)

# ── 3. Topic bütünlüğü ────────────────────────────────────────
print("\n[3] Baglan (Topic) Butunlugu")
topic_count = cur.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
check(f"En az 25 topic var ({topic_count})", topic_count >= 25)

no_topic = cur.execute("SELECT COUNT(*) FROM words WHERE topic_id IS NULL").fetchone()[0]
check("Tum kelimeler bir topice atanmis", no_topic == 0, f"({no_topic} baglamsiz)")

for slug in ["hastane", "is_hayati", "egitim", "resmi_isler", "banka", "ev_yasami", "saglik"]:
    row = cur.execute("SELECT id FROM topics WHERE slug=?", (slug,)).fetchone()
    check(f"Topic '{slug}' mevcut", row is not None)

# ── 4. Kelime kalitesi ────────────────────────────────────────
print("\n[4] Kelime Kalitesi")

noun_no_article = cur.execute(
    "SELECT COUNT(*) FROM words WHERE word_type='noun' AND article IS NULL"
).fetchone()[0]
noun_total = cur.execute("SELECT COUNT(*) FROM words WHERE word_type='noun'").fetchone()[0]
article_ratio = (noun_total - noun_no_article) / noun_total if noun_total else 0
check(f"Isimlerin >=80pct artikel sahip ({article_ratio:.0%})", article_ratio >= 0.80)

art_dist = {r[0]: r[1] for r in cur.execute(
    "SELECT article, COUNT(*) FROM words WHERE article IS NOT NULL GROUP BY article"
)}
der_c = art_dist.get("der", 0)
die_c = art_dist.get("die", 0)
das_c = art_dist.get("das", 0)
check(f"'der' en az 400 kelime ({der_c})", der_c >= 400)
check(f"'die' en az 400 kelime ({die_c})", die_c >= 400)
check(f"'das' en az 200 kelime ({das_c})", das_c >= 200)
print(f"     Artikel dagilimi: der={der_c} | die={die_c} | das={das_c}")

dupes = cur.execute("""
    SELECT word, level, source, COUNT(*) as n
    FROM words GROUP BY word, level, source HAVING n > 1
""").fetchall()
check(f"Duplikat kelime yok", len(dupes) == 0, f"({len(dupes)} duplikat)")
if dupes:
    for d in dupes[:5]:
        print(f"     Duplikat: '{d[0]}' ({d[1]}) x{d[3]}")

# ── 5. Kritik kelime varlığı ──────────────────────────────────
print("\n[5] Kritik Kelimeler (ogrenci senaryolari)")
critical = [
    ("Arzt",        "der", "Hastane"),
    ("Wohnung",     "die", "Ev yasami"),
    ("Konto",       "das", "Banka"),
    ("Bahnhof",     "der", "Seyahat"),
    ("Polizei",     "die", "Resmi isler"),
    ("Krankenhaus", "das", "Hastane"),
    ("Schule",      "die", "Egitim"),
]
for (word, art, scenario) in critical:
    row = cur.execute(
        "SELECT id FROM words WHERE word=? AND article=?", (word, art)
    ).fetchone()
    check(f"'{art} {word}' ({scenario})", row is not None)

# Fiiller
for verb in ["arbeiten", "lernen", "kaufen", "fahren", "sprechen"]:
    row = cur.execute("SELECT id FROM words WHERE word=?", (verb,)).fetchone()
    check(f"Fiil '{verb}' mevcut", row is not None)

# ── 6. FSRS tablosu yapısı ────────────────────────────────────
print("\n[6] FSRS Tablosu Yapisi")
cols = {r[1] for r in cur.execute("PRAGMA table_info(fsrs_cards)")}
for col in ["profile_id", "word_id", "stability", "difficulty", "retrievability",
            "state", "due", "reps", "lapses"]:
    check(f"Sutun '{col}' var", col in cols)

# ── 7. Index'ler ──────────────────────────────────────────────
print("\n[7] Performans Indexleri")
indexes = {r[1] for r in cur.execute("SELECT * FROM sqlite_master WHERE type='index'")}
for idx in ["idx_words_level", "idx_words_topic", "idx_fsrs_profile", "idx_fsrs_due"]:
    check(f"Index '{idx}' var", idx in indexes)

# ── 8. Uygulama senaryosu sorguları ───────────────────────────
print("\n[8] Uygulama Senaryosu Sorgulari")

# Senaryo A: A1 ogrenci, hastane konusu secildi
res_a = cur.execute("""
    SELECT w.article, w.word, t.name_tr
    FROM words w JOIN topics t ON w.topic_id = t.id
    WHERE t.slug IN ('hastane', 'saglik') AND w.level IN ('A1', 'A2')
    ORDER BY w.word
""").fetchall()
check(f"Hastane sorgusu: {len(res_a)} kelime", len(res_a) >= 5)
print("     Ornek: " + ", ".join(f"{r[0] or ''} {r[1]}" for r in res_a[:5]))

# Senaryo B: i+1 — A1 ogrenciye A2 kelimeleri
res_b = cur.execute("""
    SELECT COUNT(*) FROM words w JOIN topics t ON w.topic_id = t.id
    WHERE w.level = 'A2' AND t.slug = 'is_hayati'
""").fetchone()[0]
check(f"i+1 sorgusu (A2 is hayati): {res_b} kelime", res_b > 0)

# Senaryo C: Interleaved practice — farkli konulardan karistir
res_c = cur.execute("""
    SELECT w.word, t.slug
    FROM words w JOIN topics t ON w.topic_id = t.id
    WHERE w.level = 'A1' AND t.slug IN ('ev_yasami', 'yiyecek_icecek', 'saglik')
    ORDER BY RANDOM() LIMIT 9
""").fetchall()
check(f"Interleaved sorgu (3 farkli topic): {len(res_c)} kelime", len(res_c) == 9)
topics_hit = {r[1] for r in res_c}
print(f"     Dokulan topicler: {topics_hit}")

# Senaryo D: Artikel drill — das kelimeleri (Turkler icin zor)
res_d = cur.execute("""
    SELECT word FROM words WHERE article = 'das' AND level = 'A1'
    ORDER BY RANDOM() LIMIT 5
""").fetchall()
check(f"Das artikel drill: {len(res_d)} kelime", len(res_d) >= 3)
print("     Ornek das: " + ", ".join(r[0] for r in res_d))

# Senaryo E: FSRS simule — profil olmadan kart olusturulabilir mi?
# (Simule: join kontrolu)
res_e = cur.execute("""
    SELECT w.id, w.word, w.article, t.name_tr
    FROM words w JOIN topics t ON w.topic_id = t.id
    WHERE w.level = 'A1' AND t.slug = 'yiyecek_icecek'
    ORDER BY w.id LIMIT 5
""").fetchall()
check(f"FSRS kart adaylari hazir: {len(res_e)} kelime", len(res_e) >= 5)
print("     Ornek: " + ", ".join(f"{r[2] or ''} {r[1]}" for r in res_e))

# Senaryo F: Topic hiyerarsisi — alt-topic cekme
res_f = cur.execute("""
    SELECT t.slug, t.name_tr, t.parent_slug
    FROM topics t WHERE t.parent_slug IS NOT NULL
""").fetchall()
check(f"Alt-topic hiyerarsisi: {len(res_f)} alt-topic", len(res_f) >= 5)
for r in res_f[:4]:
    print(f"     {r[2]} -> {r[0]} ({r[1]})")

# ── 9. Veri kalite ölçütleri ──────────────────────────────────
print("\n[9] Veri Kalite Metrikleri")

# Ortalama kelime uzunlugu mantikli mi?
avg_len = cur.execute("SELECT AVG(LENGTH(word)) FROM words").fetchone()[0]
check(f"Ort. kelime uzunlugu mantikli ({avg_len:.1f} kar)", 4 < avg_len < 20)

# En kisa / uzun kelimeler
shortest = cur.execute("SELECT word, LENGTH(word) FROM words ORDER BY LENGTH(word) LIMIT 3").fetchall()
longest  = cur.execute("SELECT word, LENGTH(word) FROM words ORDER BY LENGTH(word) DESC LIMIT 3").fetchall()
print(f"     En kisa: {[r[0] for r in shortest]}")
print(f"     En uzun: {[r[0] for r in longest]}")

# Fiil orani
verb_ratio = cur.execute("SELECT COUNT(*) FROM words WHERE word_type='verb'").fetchone()[0] / total
check(f"Fiil orani mantikli ({verb_ratio:.0%}, beklenen %20-40)", 0.15 < verb_ratio < 0.45)

# ── OZET ──────────────────────────────────────────────────────
print("\n" + "="*60)
if errors:
    print(f"  {FAIL} {len(errors)} test BASARISIZ:")
    for e in errors:
        print(f"     - {e}")
    sys.exit(1)
else:
    print(f"  {PASS} Tum testler gecti! ({total} kelime, {topic_count} baglan)")

print("\nSeviye Ozeti:")
for lv in ["A1", "A2", "B1", "B2"]:
    cnt = counts.get(lv, 0)
    bar = "█" * (cnt // 100)
    print(f"  {lv}: {bar} {cnt}")
print(f"  TOPLAM: {total}")
print("="*60)
conn.close()
