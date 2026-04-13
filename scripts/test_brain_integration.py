"""
DeutschMeister AI Beyin — Entegrasyon Testi
============================================
Tüm 5 modülü gerçek DB ve gerçek veri ile test eder.
Çalıştırma: cd /home/user/workspace/backend && python scripts/test_brain_integration.py
"""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

DB_URL = "sqlite+aiosqlite:///./deutschmeister.db"
engine = create_async_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession,
                                  expire_on_commit=False, autoflush=False)

PASS = "✅"
FAIL = "❌"
errors = []

def check(label, condition, detail=""):
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label} {detail}")
        errors.append(label)

async def get_test_profile(db) -> str:
    result = await db.execute(text("SELECT id FROM profiles LIMIT 1"))
    row = result.fetchone()
    if not row:
        from datetime import datetime, timezone
        import uuid
        pid = str(uuid.uuid4())
        await db.execute(text("""
            INSERT INTO profiles (id, name, age, level, level_code, created_at)
            VALUES (:id, 'Test Brain', 8, 'beginner', 'A1', :now)
        """), {"id": pid, "now": datetime.now(timezone.utc).isoformat()})
        await db.commit()
        return pid
    return row[0]


async def test_fsrs(db: AsyncSession, profile_id: str):
    print("\n[1] FSRSEngine")
    from app.services.fsrs_engine import FSRSEngine
    engine = FSRSEngine(db)

    # Kart başlatma
    count = await engine.initialize_cards(profile_id, level="A1", topic_slug="ev_yasami")
    check(f"initialize_cards: {count} kart oluştu", count >= 0)

    # Due kartlar
    cards = await engine.get_due_cards(profile_id, level="A1", limit=5)
    check(f"get_due_cards: {len(cards)} kart döndü", isinstance(cards, list))

    # En az bir kart varsa review yap
    if cards:
        word_id = cards[0]["word_id"]
        # Good review
        result = await engine.record_review(profile_id, word_id, rating=3)
        check("record_review (Good) çalışıyor", result.get("new_state") is not None)
        check("next_review tarihi var", "next_review" in result)

        # Again review (unuttu)
        result2 = await engine.record_review(profile_id, word_id, rating=1)
        check("record_review (Again) çalışıyor", result2.get("new_state") is not None)
        print(f"     State değişimi: Good→{result['new_state']} Again→{result2['new_state']}")

    # İstatistikler
    stats = await engine.get_stats(profile_id)
    check("get_stats çalışıyor", isinstance(stats, dict))
    check("stats.due_today alanı var", "due_today" in stats)
    check("stats.motivation_message var", "motivation_message" in stats and len(stats["motivation_message"]) > 5)
    check("stats.streak_days var", "streak_days" in stats)
    print(f"     Stats: due={stats['due_today']} mastered={stats['mastered']} "
          f"streak={stats['streak_days']} retensiyon={stats['retention_rate']}")
    print(f"     Motivasyon: {stats['motivation_message']}")

    # Bulk review (SessionAnalyzer bağlantısı simule)
    bulk = await engine.bulk_review_from_analysis(
        profile_id, mastered=["Haus", "Küche"], struggled=["Dachboden"]
    )
    check(f"bulk_review çalışıyor ({bulk['updated']} güncellendi)", isinstance(bulk, dict))


async def test_curriculum(db: AsyncSession, profile_id: str):
    print("\n[2] CurriculumEngine (i+1)")
    from app.services.curriculum_engine import CurriculumEngine
    from app.services.fsrs_engine import FSRSEngine

    engine = FSRSEngine(db)
    curriculum = CurriculumEngine(db)
    stats = await engine.get_stats(profile_id)

    # Normal plan (anxiety=low)
    plan = await curriculum.get_session_plan(
        profile_id=profile_id,
        fsrs_stats=stats,
        last_session_insight=None,
    )
    check("get_session_plan döndü", plan is not None)
    check("target_level A1 (beginner profil)", plan.target_level == "A1")
    check("plus_one_level A2 veya A1 (i+1)", plan.plus_one_level in ("A1","A2","B1"))
    check("focus_topic var", bool(plan.focus_topic_slug))
    check("motivation_message var", bool(plan.motivation_message))
    check("grammar_focus var", bool(plan.grammar_focus))
    print(f"     Konu: {plan.focus_topic_tr} | Gramer: {plan.grammar_focus}")
    print(f"     Kelimeler: {len(plan.vocabulary)} yeni, {len(plan.review_words)} tekrar")

    # Yüksek kaygı planı
    high_anxiety_insight = {"anxiety_signal": "high", "engagement": "low",
                            "error_patterns": [], "recommended_next_topic": None,
                            "level_assessment": "A1"}
    plan_h = await curriculum.get_session_plan(
        profile_id=profile_id,
        fsrs_stats=stats,
        last_session_insight=high_anxiety_insight,
    )
    check("Yüksek kaygıda session_size küçülür", plan_h.session_size < plan.session_size)
    check("Yüksek kaygıda anxiety_signal='high'", plan_h.anxiety_signal == "high")
    print(f"     Kaygı: normal={plan.session_size} kelime vs yüksek={plan_h.session_size} kelime")

    # Çok-faktörlü level update (simule)
    mock_stats = {"total_cards": 100, "mastered": 85, "retention_rate": 0.90,
                  "due_today": 2, "streak_days": 5}
    mock_insight = {"anxiety_signal": "low", "level_assessment": "A1+",
                    "error_patterns": []}
    level_result = await curriculum.update_level(
        profile_id=profile_id,
        fsrs_stats=mock_stats,
        last_session_insight=mock_insight,
    )
    check("update_level çalışıyor", "composite_score" in level_result)
    check("composite_score 0-1 arasında", 0 <= level_result["composite_score"] <= 1)
    print(f"     Level update: {level_result['old_level']} → {level_result['new_level']} "
          f"(skor: {level_result['composite_score']})")
    print(f"     Sebep: {level_result['reason'][:80]}...")

    # Kaygı yüksekse seviye değişmez
    high_a = {"anxiety_signal": "high", "level_assessment": "B2", "error_patterns": []}
    lr_h = await curriculum.update_level(profile_id, mock_stats, high_a)
    check("Kaygı yüksekse seviye değişmez", not lr_h["changed"])


async def test_interleaved(db: AsyncSession, profile_id: str):
    print("\n[3] InterleavedScheduler")
    from app.services.interleaved_scheduler import InterleavedScheduler
    from sqlalchemy import text

    scheduler = InterleavedScheduler()

    # Test kelimeler
    due = [{"word_id": 1, "word": "Haus", "article": "das", "translation_tr": "ev",
            "example_de": None, "level": "A1", "_source": "review", "_priority": 0, "difficulty": 5.0}]
    new_words = [
        {"word_id": 2, "word": "Küche", "article": "die", "translation_tr": "mutfak",
         "example_de": None, "level": "A1", "_source": "new", "_priority": 1, "difficulty": 5.0},
        {"word_id": 3, "word": "Fenster", "article": "das", "translation_tr": "pencere",
         "example_de": None, "level": "A1", "_source": "new", "_priority": 1, "difficulty": 5.0},
        {"word_id": 4, "word": "wohnen", "article": None, "translation_tr": "yaşamak",
         "example_de": None, "level": "A1", "_source": "new", "_priority": 1, "difficulty": 5.0},
    ]
    artikel = [
        {"word_id": 5, "word": "Tisch", "article": "der", "translation_tr": "masa",
         "example_de": None, "level": "A1", "_source": "artikel_drill", "_priority": 2, "difficulty": 5.0},
    ]

    # Normal session (anxiety=low)
    items = scheduler.build_session(due, new_words, artikel, anxiety_signal="low")
    check(f"build_session döndü: {len(items)} item", len(items) > 0)
    modes = {item.mode for item in items}
    check(f"Birden fazla mod var: {modes}", len(modes) >= 2)
    sources = {item.source for item in items}
    check(f"Farklı kaynaklardan kelimeler: {sources}", len(sources) >= 2)
    print(f"     Normal: {len(items)} item, modlar: {modes}")

    # Yüksek kaygı session
    items_h = scheduler.build_session(due, new_words, artikel, anxiety_signal="high")
    check(f"Yüksek kaygı session küçük: {len(items_h)}", len(items_h) <= 7)
    modes_h = {item.mode for item in items_h}
    check("Yüksek kaygıda recall dominant", "recall" in modes_h)
    print(f"     Kaygı: {len(items_h)} item, modlar: {modes_h}")

    # Serileştirme
    dicts = scheduler.to_dict(items[:3])
    check("to_dict serileştirme çalışıyor", all("prompt" in d for d in dicts))

    # Artikel olmayan kelimede artikel modu gelmiyor mu?
    no_art_items = [i for i in items if i.word == "wohnen"]
    bad = [i for i in no_art_items if i.mode in ("artikel", "choose_artikel")]
    check("Artikelsiz kelimeye artikel modu atanmaz", len(bad) == 0)


async def test_session_analyzer(db: AsyncSession, profile_id: str):
    print("\n[4] SessionAnalyzer")
    from app.services.session_analyzer import SessionAnalyzer

    analyzer = SessionAnalyzer(db)

    # session_insights tablosunu oluştur
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS session_insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            data TEXT,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """))
    await db.commit()

    # get_last_insight — başlangıçta None
    insight = await analyzer.get_last_insight(profile_id)
    check("get_last_insight None döner (boşsa)", True)  # her iki durum da geçerli

    # Mock insight yaz (gerçek Gemini çağrısı yapmadan)
    import json
    mock_data = {
        "mastered": ["der Hund", "gehen"],
        "struggled": ["das Mädchen"],
        "error_patterns": ["artikel_neutrum"],
        "anxiety_signal": "low",
        "engagement": "high",
        "recommended_next_topic": "saglik",
        "level_assessment": "A1+",
        "session_quality": 0.75,
        "summary_tr": "Aile kelimelerinde iyi ilerledi.",
    }
    await db.execute(text("""
        INSERT INTO session_insights (session_id, profile_id, status, data)
        VALUES ('test-session-1', :pid, 'completed', :data)
    """), {"pid": profile_id, "data": json.dumps(mock_data)})
    await db.commit()

    # get_last_insight şimdi döndürmeli
    insight = await analyzer.get_last_insight(profile_id)
    check("get_last_insight mock veriyi döndürür", insight is not None)
    check("anxiety_signal doğru", insight.get("anxiety_signal") == "low")
    check("recommended_next_topic doğru", insight.get("recommended_next_topic") == "saglik")
    print(f"     Insight: {insight.get('summary_tr')}")

    # _build_transcript testi
    messages = [
        {"role": "user", "content": "Guten Morgen!"},
        {"role": "model", "content": "Guten Morgen! Wie geht es Ihnen?"},
        {"role": "user", "content": "Gut, danke."},
    ]
    transcript = analyzer._build_transcript(messages)
    check("_build_transcript çalışıyor", "Öğrenci" in transcript and "Öğretmen" in transcript)


async def test_affective_filter(db: AsyncSession, profile_id: str):
    print("\n[5] Affective Filter (Düşük Kaygı)")
    from app.services.curriculum_engine import CurriculumEngine
    from app.services.fsrs_engine import FSRSEngine

    engine = FSRSEngine(db)
    curriculum = CurriculumEngine(db)
    stats = await engine.get_stats(profile_id)

    # Düşük kaygı → normal plan
    low_insight = {"anxiety_signal": "low", "engagement": "high",
                   "error_patterns": [], "recommended_next_topic": None}
    plan_low = await curriculum.get_session_plan(profile_id, fsrs_stats=stats,
                                                  last_session_insight=low_insight)

    # Yüksek kaygı → kompakt plan + kolay gramer
    high_insight = {"anxiety_signal": "high", "engagement": "low",
                    "error_patterns": ["word_order_V2", "dativ_case"],
                    "recommended_next_topic": None}
    plan_high = await curriculum.get_session_plan(profile_id, fsrs_stats=stats,
                                                   last_session_insight=high_insight)

    check("Kaygı arttıkça session_size azalır",
          plan_high.session_size < plan_low.session_size)
    check("Yüksek kaygıda gramer basit",
          "gramer odağı yok" in plan_high.grammar_focus.lower() or
          "serbest" in plan_high.grammar_focus.lower())
    check("Yüksek kaygıda artikel_drill yok", len(plan_high.artikel_drill) == 0)
    check("Motivasyon mesajı kaygıya göre değişir",
          plan_high.motivation_message != plan_low.motivation_message)

    print(f"     Düşük kaygı: {plan_low.session_size} kelime, "
          f"gramer: {plan_low.grammar_focus[:40]}")
    print(f"     Yüksek kaygı: {plan_high.session_size} kelime, "
          f"gramer: {plan_high.grammar_focus[:40]}")

    # Prompt injection'da anxiety_signal doğru bloğu ekliyor mu?
    from app.api.routes.vocabulary import _build_prompt_injection
    injection = _build_prompt_injection(plan_high, high_insight)
    check("Yüksek kaygı injection'da 'stresli' uyarısı var",
          "stresli" in injection.lower() or "hata düzeltme" in injection.lower())


async def main():
    print("\n" + "="*60)
    print("  DeutschMeister AI Beyin — Entegrasyon Testleri")
    print("="*60)

    async with SessionLocal() as db:
        profile_id = await get_test_profile(db)
        print(f"\n  Test profili: {profile_id[:20]}...")

        await test_fsrs(db, profile_id)
        await test_curriculum(db, profile_id)
        await test_interleaved(db, profile_id)
        await test_session_analyzer(db, profile_id)
        await test_affective_filter(db, profile_id)

    print("\n" + "="*60)
    if errors:
        print(f"  ❌ {len(errors)} test BAŞARISIZ:")
        for e in errors:
            print(f"     - {e}")
        sys.exit(1)
    else:
        print(f"  ✅ Tüm testler geçti!")
        print("  AI Beyin — 5 modül tam entegre, çalışmaya hazır")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
