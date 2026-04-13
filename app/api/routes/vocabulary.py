"""
Vocabulary API — FSRS + Curriculum + Interleaved Practice
==========================================================
GET  /api/vocabulary/due/{profile_id}     → Bugünkü tekrar listesi (FSRS)
GET  /api/vocabulary/topics               → Tüm topic listesi
POST /api/vocabulary/review               → Kelime değerlendirmesi (FSRS güncelle)
GET  /api/sessions/plan/{profile_id}      → Oturum planı (CurriculumEngine)
POST /api/sessions/end                    → Oturumu bitir + SessionAnalyzer tetikle
GET  /api/profile/{profile_id}/stats      → Öğrenme istatistikleri
GET  /api/sessions/feedback/{profile_id} → Fossilization feedback (Türkçe hata açıklamaları + risk)
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal, get_db
from app.services.curriculum_engine import CurriculumEngine
from app.services.fsrs_engine import FSRSEngine
from app.services.interleaved_scheduler import InterleavedScheduler
from app.services.session_analyzer import SessionAnalyzer

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Pydantic modeller ──────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    profile_id: str
    word_id: int
    rating: int          # 1=Tekrar(Again) 2=Zor(Hard) 3=İyi(Good) 4=Kolay(Easy)


class SessionEndRequest(BaseModel):
    profile_id: str
    session_id: str
    messages: Optional[list[dict]] = None  # yoksa DB'den çekilir


class InitCardsRequest(BaseModel):
    profile_id: str
    level: str           # A1|A2|B1|B2
    topic_slug: Optional[str] = None


# ── GET /api/vocabulary/topics ─────────────────────────────────────────────
from sqlalchemy import text

@router.get("/vocabulary/topics")
async def get_topics(
    level: Optional[str] = Query(None, description="A1|A2|B1|B2"),
    db: AsyncSession = Depends(get_db),
):
    """Tüm bağlam (topic) listesi — opsiyonel seviye filtresi."""
    level_filter = "WHERE t.min_level <= :level" if level else ""
    params = {"level": level} if level else {}
    result = await db.execute(text(f"""
        SELECT t.slug, t.name_de, t.name_tr, t.description_tr, t.min_level,
               t.parent_slug,
               COUNT(w.id) as word_count
        FROM topics t
        LEFT JOIN words w ON w.topic_id = t.id
        {level_filter}
        GROUP BY t.id
        ORDER BY t.min_level, t.name_tr
    """), params)
    rows = result.fetchall()
    return {
        "topics": [
            {
                "slug": r[0],
                "name_de": r[1],
                "name_tr": r[2],
                "description_tr": r[3],
                "min_level": r[4],
                "parent_slug": r[5],
                "word_count": r[6],
            }
            for r in rows
        ],
        "total": len(rows),
    }


# ── GET /api/vocabulary/due/{profile_id} ───────────────────────────────────
@router.get("/vocabulary/due/{profile_id}")
async def get_due_cards(
    profile_id: str,
    topic_slug: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Bugün tekrar edilmesi gereken kelimeler (FSRS due kartlar)."""
    engine = FSRSEngine(db)
    cards = await engine.get_due_cards(
        profile_id=profile_id,
        topic_slug=topic_slug,
        level=level,
        limit=limit,
    )
    return {"cards": cards, "count": len(cards)}


# ── POST /api/vocabulary/review ────────────────────────────────────────────
@router.post("/vocabulary/review")
async def review_word(
    req: ReviewRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Kelime değerlendirmesi → FSRS kart güncelleme.
    rating: 1=Tekrar | 2=Zor | 3=İyi | 4=Kolay
    """
    if req.rating not in (1, 2, 3, 4):
        raise HTTPException(status_code=400, detail="rating 1-4 arasında olmalı")

    engine = FSRSEngine(db)
    result = await engine.record_review(
        profile_id=req.profile_id,
        word_id=req.word_id,
        rating=req.rating,
    )
    return result


# ── POST /api/vocabulary/init ──────────────────────────────────────────────
@router.post("/vocabulary/init")
async def init_cards(
    req: InitCardsRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Profil için ilk FSRS kartlarını oluşturur.
    Yeni profil oluşturulduğunda veya konu değiştirildiğinde çağrılır.
    """
    engine = FSRSEngine(db)
    count = await engine.initialize_cards(
        profile_id=req.profile_id,
        level=req.level,
        topic_slug=req.topic_slug,
    )
    return {"initialized": count, "level": req.level, "topic_slug": req.topic_slug}


# ── GET /api/sessions/plan/{profile_id} ────────────────────────────────────
@router.get("/sessions/plan/{profile_id}")
async def get_session_plan(
    profile_id: str,
    topic_slug: Optional[str] = Query(None, description="İstenen konu (opsiyonel)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Oturum planı oluşturur.
    CurriculumEngine + InterleavedScheduler entegre çalışır.
    """
    engine = FSRSEngine(db)
    curriculum = CurriculumEngine(db)
    analyzer = SessionAnalyzer(db)
    scheduler = InterleavedScheduler()

    # Son oturum analizi varsa kullan
    last_insight = await analyzer.get_last_insight(profile_id)

    # FSRS istatistikleri
    fsrs_stats = await engine.get_stats(profile_id)

    # Curriculum planı
    plan = await curriculum.get_session_plan(
        profile_id=profile_id,
        requested_topic=topic_slug,
        fsrs_stats=fsrs_stats,
        last_session_insight=last_insight,
    )

    # Interleaved session
    session_items = scheduler.build_session(
        due_cards=plan.review_words,
        new_words=plan.vocabulary,
        artikel_drill=plan.artikel_drill,
        anxiety_signal=plan.anxiety_signal,
        engagement=last_insight.get("engagement", "medium") if last_insight else "medium",
    )

    return {
        "profile_id": profile_id,
        "target_level": plan.target_level,
        "plus_one_level": plan.plus_one_level,
        "focus_topic": {
            "slug": plan.focus_topic_slug,
            "name_tr": plan.focus_topic_tr,
        },
        "grammar_focus": plan.grammar_focus,
        "session_size": plan.session_size,
        "anxiety_signal": plan.anxiety_signal,
        "motivation_message": plan.motivation_message,
        "items": scheduler.to_dict(session_items),
        "stats": {
            "due_today": fsrs_stats.get("due_today", 0),
            "mastered": fsrs_stats.get("mastered", 0),
            "retention_rate": fsrs_stats.get("retention_rate", 0),
        },
        # Sistem prompt'a enjekte edilecek raw plan
        "system_prompt_injection": _build_prompt_injection(plan, last_insight),
    }


# ── POST /api/sessions/end ─────────────────────────────────────────────────
@router.post("/sessions/end")
async def end_session(
    req: SessionEndRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """
    Oturumu bitirir.
    SessionAnalyzer arka planda (BackgroundTask) çalışır — kullanıcı beklemez.
    """
    analyzer = SessionAnalyzer(db)

    # BackgroundTask — kullanıcı beklemez, analiz arka planda
    background_tasks.add_task(
        _run_analysis_background,
        profile_id=req.profile_id,
        session_id=req.session_id,
        messages=req.messages,
    )

    return {
        "status": "ok",
        "message": "Oturum kaydedildi. Analiz arka planda yapılıyor.",
        "session_id": req.session_id,
    }


async def _run_analysis_background(
    profile_id: str,
    session_id: str,
    messages: list[dict] | None,
):
    """BackgroundTask için ayrı DB session açar."""
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        analyzer = SessionAnalyzer(db)
        try:
            await analyzer.analyze_session(
                profile_id=profile_id,
                session_id=session_id,
                messages=messages,
            )
            # Seviye güncellemesi de burada
            fsrs_engine = FSRSEngine(db)
            curriculum = CurriculumEngine(db)
            stats = await fsrs_engine.get_stats(profile_id)
            last_insight = await analyzer.get_last_insight(profile_id)
            await curriculum.update_level(
                profile_id=profile_id,
                fsrs_stats=stats,
                last_session_insight=last_insight,
            )
        except Exception as e:
            logger.error(f"Background analiz hatası: profile={profile_id} hata={e}")


# ── GET /api/profile/{profile_id}/stats ───────────────────────────────────
@router.get("/profile/{profile_id}/stats")
async def get_profile_stats(
    profile_id: str,
    level: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Öğrenme istatistikleri — frontend progress ekranı için.
    Motivasyon mesajı dahil.
    """
    engine = FSRSEngine(db)
    stats = await engine.get_stats(profile_id, level=level)

    analyzer = SessionAnalyzer(db)
    last_insight = await analyzer.get_last_insight(profile_id)

    return {
        **stats,
        "last_session": {
            "topic": last_insight.get("recommended_next_topic") if last_insight else None,
            "level_assessment": last_insight.get("level_assessment") if last_insight else None,
            "anxiety_signal": last_insight.get("anxiety_signal", "low") if last_insight else "low",
            "summary": last_insight.get("summary_tr") if last_insight else None,
        } if last_insight else None,
    }


# ── GET /api/sessions/feedback/{profile_id} ──────────────────────────────

# Fossilization: error_pattern → Türkçe açıklama haritası
# Her hata: başlık, kısa açıklama ve pratik ipucu
_ERROR_EXPLANATIONS: dict[str, dict] = {
    "artikel_neutrum": {
        "baslik": "das — Nötr Artikel Hatası",
        "aciklama": "'das' gerektiren isimleri 'der' veya 'die' ile kullandın.",
        "ipucu": "Türkçede artikel yoktur, bu yüzden Almanca artikeller en büyük zorluklardan biridir. 'das Kind', 'das Haus', 'das Auto' gibi nötr isimleri özel olarak ezberle.",
    },
    "artikel_maskulin": {
        "baslik": "der — Eril Artikel Hatası",
        "aciklama": "'der' gerektiren isimleri yanlış artikelle kullandın.",
        "ipucu": "Erkekler, meslekler ve haftanın günleri genellikle 'der' alır: der Mann, der Arzt, der Montag.",
    },
    "artikel_feminin": {
        "baslik": "die — Dişil Artikel Hatası",
        "aciklama": "'die' gerektiren isimleri yanlış artikelle kullandın.",
        "ipucu": "-ung, -heit, -keit, -schaft ile biten isimler HER ZAMAN 'die' alır: die Zeitung, die Gesundheit.",
    },
    "word_order_V2": {
        "baslik": "Fiil İkinci Sırada (V2) Kuralı",
        "aciklama": "Almancada fiil her zaman cümlenin ikinci pozisyonunda olmalı, ama bunu atladın.",
        "ipucu": "Türkçede fiil sona gelir (SOV), Almancada ise ikinciye gelir (V2). 'Heute gehe ich' doğru, 'Heute ich gehe' yanlış.",
    },
    "dativ_case": {
        "baslik": "Datif (Yönelme) Hâli Hatası",
        "aciklama": "'mit', 'nach', 'bei', 'von', 'zu' gibi edatlardan sonra datif kullanmadın.",
        "ipucu": "Bu edatlar DAIMA datif ister: mit dem Mann (der→dem), mit der Frau (değişmez), mit dem Kind (das→dem).",
    },
    "akkusativ_case": {
        "baslik": "Akuzatif (Belirtme) Hâli Hatası",
        "aciklama": "Nesne konumundaki isimlerde akuzatif kullanmadın.",
        "ipucu": "Sadece eril 'der' değişir: der→den. 'Ich sehe den Mann' (der Mann → den Mann).",
    },
    "plural_form": {
        "baslik": "Çoğul Eklerinde Hata",
        "aciklama": "İsimlerin çoğul biçimini yanlış oluşturdun.",
        "ipucu": "Almancada çoğul çok düzensizdir, her ismi artikeli ve çoğuluyla birlikte öğren: der Hund → die Hunde.",
    },
    "verb_conjugation": {
        "baslik": "Fiil Çekiminde Hata",
        "aciklama": "Özneye göre doğru fiil ekini kullanmadın.",
        "ipucu": "ich→-e, du→-st, er/sie/es→-t, wir→-en, ihr→-t, sie/Sie→-en. Düzensiz fiilleri (sein, haben) ayrıca ezberle.",
    },
    "verb_position": {
        "baslik": "Fiil Konumu Hatası",
        "aciklama": "Yan cümlede veya modal fiil kullanımında fiili yanlış konuma koydun.",
        "ipucu": "'weil', 'dass', 'wenn' gibi bağlaçlardan sonra fiil SONA gider: 'weil ich müde bin' doğru.",
    },
    "umlaut_pronunciation": {
        "baslik": "Umlaut Telaffuz Hatası",
        "aciklama": "ü, ö, ä seslerini doğru telaffuz etmekte zorlandın.",
        "ipucu": "ü: dudakları 'o' gibi yuvarlat, 'i' de. ö: dudakları 'o' gibi yuvarlat, 'e' de. Telaffuz ekranında özel pratik yap.",
    },
    "compound_word": {
        "baslik": "Bileşik Kelime Hatası",
        "aciklama": "Almanca bileşik kelimeleri (Komposita) yanlış oluşturdun veya anlamlandırdın.",
        "ipucu": "Almancada kelimeler birleştirilerek yeni anlam oluşturulur. Son kelime ana anlam verir ve artikeli belirler: das Kranken+haus → das Krankenhaus (hastane).",
    },
}


@router.get("/sessions/feedback/{profile_id}")
async def get_session_feedback(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Son oturum analizinden fossilization feedback döner.
    Frontend oturum bittikten sonra bu endpoint'i çeker.

    Döner:
      - error_explanations: Türkçe açıklamalı hata listesi
      - struggled_words: zorlandığı kelimeler
      - mastered_words: öğrenilen kelimeler
      - summary_tr: oturum özeti
      - anxiety_signal: kaygı seviyesi
      - session_quality: oturum kalitesi (0-1)
      - has_feedback: analiz tamamlandı mı
    """
    analyzer = SessionAnalyzer(db)
    insight = await analyzer.get_last_insight(profile_id)

    if not insight:
        return {
            "has_feedback": False,
            "message": "Henüz tamamlanmış oturum analizi yok.",
        }

    # error_patterns → Türkçe açıklamaya dönüştür
    raw_patterns = insight.get("error_patterns", [])
    explanations = []
    for pattern in raw_patterns:
        if pattern in _ERROR_EXPLANATIONS:
            exp = _ERROR_EXPLANATIONS[pattern]
            explanations.append({
                "pattern": pattern,
                "baslik": exp["baslik"],
                "aciklama": exp["aciklama"],
                "ipucu": exp["ipucu"],
            })

    # Fossilization riski: aynı hata birden fazla oturumda tekrarlandı mı?
    # Son 3 oturumun analizini çek
    recent_result = await db.execute(text("""
        SELECT data FROM session_insights
        WHERE profile_id = :pid AND status = 'completed'
        ORDER BY created_at DESC LIMIT 3
    """), {"pid": profile_id})
    recent_rows = recent_result.fetchall()

    # Hangi hatalar birden fazla oturumda tekrarlandı?
    pattern_counts: dict[str, int] = {}
    for row in recent_rows:
        try:
            past = json.loads(row[0])
            for p in past.get("error_patterns", []):
                pattern_counts[p] = pattern_counts.get(p, 0) + 1
        except Exception:
            pass

    # 2+ oturumda tekrarlanan hatalar → fossilization riski
    fossilization_risk = [
        p for p, count in pattern_counts.items() if count >= 2
    ]

    # Açıklamalara fossilization uyarısı ekle
    for exp in explanations:
        exp["fossilization_risk"] = exp["pattern"] in fossilization_risk

    return {
        "has_feedback": True,
        "summary_tr": insight.get("summary_tr", ""),
        "session_quality": insight.get("session_quality", 0.0),
        "anxiety_signal": insight.get("anxiety_signal", "low"),
        "error_explanations": explanations,
        "fossilization_risk_patterns": fossilization_risk,
        "struggled_words": insight.get("struggled", []),
        "mastered_words": insight.get("mastered", []),
        "level_assessment": insight.get("level_assessment"),
        "recommended_next_topic": insight.get("recommended_next_topic"),
    }


# ── Yardımcı: sistem prompt enjeksiyonu ───────────────────────────────────
def _build_prompt_injection(
    plan,
    last_insight: dict | None,
    agent_strategy: dict | None = None,
    weekly_grammar_target: str | None = None,
) -> str:
    """
    Chat sistem prompt'una enjekte edilecek dinamik blok.
    Layer 1 (Constitution — değişmez pedagojik kısıtlar) +
    Layer 2 (Agent Strategy — öğrenciye özgü adaptif öğretmen notları)
    chat.py bu değeri system prompt'un başına ekler.
    """
    vocab_list = ", ".join(
        f"{w.get('article', '') or ''} {w.get('word', '')}".strip()
        for w in plan.vocabulary[:8]
    )
    review_list = ", ".join(
        f"{w.get('article', '') or ''} {w.get('word', '')}".strip()
        for w in plan.review_words[:5]
    )
    artikel_list = ", ".join(
        f"({w.get('article', '?')}) {w.get('word', '')}".strip()
        for w in plan.artikel_drill
    )

    anxiety_instruction = {
        "low": "Normal öğretim modu. Hataları nazikçe düzelt, i+1 seviyesinde challenge yap.",
        "medium": "Öğrenci biraz zorlanıyor. Nazik ol, başarıları vurgula, basit dil kullan.",
        "high": (
            "ÖNEMLİ: Öğrenci şu an stresli. ASLA hata düzeltme yapma. "
            "Sadece pozitif pekiştirme. Çok kısa, kolay cümleler. "
            "Öğrencinin söylediği her şeyi takdir et."
        ),
    }.get(plan.anxiety_signal, "Normal öğretim modu.")

    # ── Layer 2: Agent Strategy (mutable) ────────────────────────────────
    strategy = agent_strategy or {}
    tutor_obs = strategy.get("tutor_observations", "")
    eng_hooks = strategy.get("engagement_hooks", [])
    scaffolding = strategy.get("scaffolding_style", "Direct")
    emotional_cal = strategy.get("emotional_calibration", "neutral")

    scaffolding_instruction = {
        "Socratic": "Cevapları direkt verme. Sorularla yönlendir. 'Örneğin, bu cümlede ne eksik?' tarzında sor.",
        "Direct": "Net ve doğrudan öğret. Kuralı açıkla, örnek ver, tekrarlattır.",
        "Playful": "Oyunsu bir ton kullan. Kelime oyunları, kısa hikayeler, yarışma biçiminde sun.",
        "Structured": "Adım adım ilerle. Her konsepti sırayla tamamla, atlama.",
    }.get(scaffolding, "Net ve doğrudan öğret.")

    hooks_str = ", ".join(eng_hooks) if eng_hooks else "henüz bilinmiyor"

    # ── Layer 1: Constitution (immutable) ────────────────────────────────
    grammar_target = weekly_grammar_target or plan.grammar_focus

    return f"""[═ PEDAGOJİK ANAYASA — ASLA İHLAL ETME ═]
CEFR Seviyesi: {plan.target_level} | i+1 Sınır: {plan.plus_one_level}
Haftalık Gramer Hedefi: {grammar_target}
Kelime Kapsamı: Sadece Goethe/CEFR onaylı kelimeler. Kapsam: {vocab_list or 'konu odaklı seç'}
Hata Düzeltme: Her seansta min 2 kritik hatayı seans sonu Türkçe açıklamayla düzelt.
L1 Girişimi: Türkçe SOV yapısını izle. Almanca V2 kuralını açıkça modelle.
Kaygı Durumu: {plan.anxiety_signal} — {anxiety_instruction}

[═ ADAPTİF ÖĞRETMEN NOTLARI — BU ÖĞRENCİYE ÖZGÜ ═]
Önceki Seans Gözlemi: {tutor_obs or 'Henüz veri yok, bu ilk seans.'}
İlgi Alanları: {hooks_str} — örneklerini bu konulardan seç.
Öğretme Tarzı: {scaffolding} — {scaffolding_instruction}
Duygusal Kalibrasyon: {emotional_cal}

[OTURUM PLANI]
Oturum Konusu: {plan.focus_topic_tr}
Tekrar Edilecekler: {review_list or 'yok'}
Artikel Pratiği: {artikel_list or 'yok'}

[GENEL KURALLAR]
- Artikel'i HER ZAMAN kelimeyle birlikte göster (der Hund, asla sadece Hund)
- Yeni kelimeyi önce Almanca yaz, sonra Türkçe çevirisini ver
- Her 3-4 mesajda bir öğrencinin anladığını kontrol et
- Motivasyon mesajı ile başla: {plan.motivation_message}
"""
