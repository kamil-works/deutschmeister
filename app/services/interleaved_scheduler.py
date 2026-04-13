"""
InterleavedScheduler — Karışık Pratik Zamanlayıcı
===================================================
MIT araştırması: A1B1C1A2B2C2... formatı, blok çalışmaya göre
uzun vadeli tutmayı ve transferi önemli ölçüde artırır.

Dinamik session_size:
  - anxiety_signal="high"   → 5-7 kelime, sadece recall modu
  - anxiety_signal="medium" → 10 kelime, recall + artikel
  - anxiety_signal="low"    → 15 kelime, tüm modlar
  - engagement="high"       → +3 bonus kelime
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Literal

# Mod tanımları
Mode = Literal["recall", "artikel", "sentence", "fill_blank", "choose_artikel"]


@dataclass
class SessionItem:
    word_id: int
    word: str
    article: str | None
    translation_tr: str | None
    example_de: str | None
    level: str
    mode: Mode
    prompt: str             # Öğrenciye gösterilecek soru/görev
    expected: str           # Beklenen cevap (değerlendirme için)
    source: str             # "review" | "new" | "artikel_drill"
    difficulty: float       # FSRS difficulty (1-10) veya 5.0 default


# Modlar arası ağırlık — anxiety ve engagement'a göre ayarlanır
MODE_WEIGHTS_LOW_ANXIETY = {
    "recall": 0.30,
    "artikel": 0.25,
    "sentence": 0.20,
    "fill_blank": 0.15,
    "choose_artikel": 0.10,
}

MODE_WEIGHTS_MEDIUM_ANXIETY = {
    "recall": 0.50,
    "artikel": 0.30,
    "fill_blank": 0.20,
    "sentence": 0.0,
    "choose_artikel": 0.0,
}

MODE_WEIGHTS_HIGH_ANXIETY = {
    "recall": 0.70,
    "artikel": 0.30,
    "fill_blank": 0.0,
    "sentence": 0.0,
    "choose_artikel": 0.0,
}

ANXIETY_MODE_WEIGHTS = {
    "low": MODE_WEIGHTS_LOW_ANXIETY,
    "medium": MODE_WEIGHTS_MEDIUM_ANXIETY,
    "high": MODE_WEIGHTS_HIGH_ANXIETY,
}

# Temel session boyutları
BASE_SIZES = {
    "high": 6,
    "medium": 10,
    "low": 15,
}

ENGAGEMENT_BONUS = {
    "high": 3,
    "medium": 0,
    "low": -2,
}


class InterleavedScheduler:
    """
    Kullanım:
        scheduler = InterleavedScheduler()
        items = scheduler.build_session(
            due_cards=fsrs_due_words,
            new_words=curriculum_new_words,
            artikel_drill=curriculum_artikel_words,
            anxiety_signal="low",
            engagement="medium",
        )
    """

    def build_session(
        self,
        due_cards: list[dict],
        new_words: list[dict],
        artikel_drill: list[dict],
        anxiety_signal: str = "low",
        engagement: str = "medium",
        forced_size: int | None = None,
    ) -> list[SessionItem]:
        """
        Kelime gruplarını interleaved formatta birleştirir.

        Algoritma:
          1. Hedef session_size hesapla
          2. Her gruptan orantılı kelime al
          3. A1B1C1A2B2C2... sırasına diz
          4. Her kelimeye uygun mod ata
        """
        # 1. Session boyutu
        if forced_size:
            session_size = forced_size
        else:
            base = BASE_SIZES.get(anxiety_signal, 15)
            bonus = ENGAGEMENT_BONUS.get(engagement, 0)
            session_size = max(3, base + bonus)

        # 2. Mod ağırlıkları
        mode_weights = ANXIETY_MODE_WEIGHTS.get(anxiety_signal, MODE_WEIGHTS_LOW_ANXIETY)
        available_modes = [m for m, w in mode_weights.items() if w > 0]
        mode_probs = [mode_weights[m] for m in available_modes]

        # 3. Grupları etiketle
        tagged: list[dict] = []
        for w in due_cards:
            tagged.append({**w, "_source": "review", "_priority": 0})
        for w in new_words:
            tagged.append({**w, "_source": "new", "_priority": 1})
        for w in artikel_drill:
            tagged.append({**w, "_source": "artikel_drill", "_priority": 2})

        # 4. Session boyutunu aşmayacak şekilde seç
        # Önce review (en öncelikli), sonra new, sonra artikel_drill
        selected: list[dict] = []
        for prio in [0, 1, 2]:
            group = [w for w in tagged if w["_priority"] == prio]
            slots = session_size - len(selected)
            if slots <= 0:
                break
            # Yüksek kaygıda review kelimelerinden de az al
            if anxiety_signal == "high" and prio == 0:
                slots = min(slots, 2)
            selected.extend(group[:slots])

        if not selected:
            return []

        # 5. Interleaved sıralama: grupları döngüsel olarak karıştır
        interleaved = self._interleave(selected)

        # 6. Her kelimeye mod ata
        session_items: list[SessionItem] = []
        for i, word in enumerate(interleaved):
            # Artikel varsa artikel modu ağırlığını artır
            has_article = bool(word.get("article"))
            # Review kelimeler için daha zorlu mod
            is_review = word["_source"] == "review"

            mode = self._pick_mode(
                available_modes=available_modes,
                probs=mode_probs,
                has_article=has_article,
                is_review=is_review,
                anxiety=anxiety_signal,
                position=i,
            )

            item = self._build_item(word, mode)
            session_items.append(item)

        return session_items

    def _interleave(self, words: list[dict]) -> list[dict]:
        """
        review(R), new(N), artikel(A) → R1 N1 A1 R2 N2 A2 ...
        Eksik grup varsa mevcut olanlarla devam et.
        """
        groups = {
            "review": [w for w in words if w["_source"] == "review"],
            "new": [w for w in words if w["_source"] == "new"],
            "artikel_drill": [w for w in words if w["_source"] == "artikel_drill"],
        }
        order = ["review", "new", "artikel_drill"]
        indices = {k: 0 for k in order}
        result = []
        total = len(words)

        while len(result) < total:
            added = False
            for src in order:
                if indices[src] < len(groups[src]):
                    result.append(groups[src][indices[src]])
                    indices[src] += 1
                    added = True
            if not added:
                break  # Tüm gruplar tükendi

        return result

    def _pick_mode(
        self,
        available_modes: list,
        probs: list,
        has_article: bool,
        is_review: bool,
        anxiety: str,
        position: int,
    ) -> Mode:
        """Bağlama göre uygun modu seç."""
        # İlk kelime her zaman recall (giriş yumuşatma)
        if position == 0 and anxiety == "high":
            return "recall"

        # Artikel olmayan kelimeler için artikel modu yasak
        if not has_article:
            filtered = [(m, p) for m, p in zip(available_modes, probs)
                        if m not in ("artikel", "choose_artikel")]
            if filtered:
                modes, ps = zip(*filtered)
                total = sum(ps)
                normalized = [p / total for p in ps]
                return random.choices(list(modes), weights=normalized, k=1)[0]

        return random.choices(available_modes, weights=probs, k=1)[0]

    def _build_item(self, word: dict, mode: Mode) -> SessionItem:
        w = word.get("word", "")
        art = word.get("article")
        tr = word.get("translation_tr") or "?"
        ex = word.get("example_de")
        article_display = f"{art} {w}" if art else w

        prompts_expected = {
            "recall": (
                f"Bu kelimenin anlamı nedir? → {article_display}",
                tr,
            ),
            "artikel": (
                f"'{w}' — der, die veya das?",
                art or "?",
            ),
            "choose_artikel": (
                f"'{w}' için doğru artikeli seç: der / die / das",
                art or "?",
            ),
            "sentence": (
                f"'{article_display}' kelimesiyle kısa bir Almanca cümle kur.",
                ex or f"Örnek: {article_display} ist wichtig.",
            ),
            "fill_blank": (
                f"Boşluğu doldurun: ___ {w} ({tr})",
                art or w,
            ),
        }

        prompt, expected = prompts_expected.get(mode, (f"Ne anlama gelir: {w}?", tr))

        return SessionItem(
            word_id=word.get("word_id", 0),
            word=w,
            article=art,
            translation_tr=tr,
            example_de=ex,
            level=word.get("level", "A1"),
            mode=mode,
            prompt=prompt,
            expected=expected,
            source=word.get("_source", "new"),
            difficulty=float(word.get("difficulty", 5.0)),
        )

    def to_dict(self, items: list[SessionItem]) -> list[dict]:
        """Serileştirme — API response için"""
        return [
            {
                "word_id": item.word_id,
                "word": item.word,
                "article": item.article,
                "translation_tr": item.translation_tr,
                "example_de": item.example_de,
                "level": item.level,
                "mode": item.mode,
                "prompt": item.prompt,
                "expected": item.expected,
                "source": item.source,
                "difficulty": item.difficulty,
            }
            for item in items
        ]
