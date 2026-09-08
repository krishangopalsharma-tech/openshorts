"""Hinglish romanisation — Devanagari speech written in Latin letters.

Whisper transcribes Hindi/Urdu in Devanagari. Every caption preset in
``caption_styles`` is a Latin display face (Anton, Bangers, Bebas…), so a
Devanagari caption drops out of the preset into whatever libass finds — at
best the plain Noto Sans Devanagari in ``fonts/``, at worst tofu boxes.
Romanising the transcript keeps the presets: captions read "aap kaise hain",
which is how the audience types the language anyway.

It is deliberately a *readable approximation*, not academic transliteration
(the rules are ClipForge's, whose output was checked against real Hindi
speech):

  1. Devanagari is an abugida: a bare consonant carries an inherent short
     ``a``, a matra replaces it, and virama (``्``) removes it. ``_ROMAN``
     below is the per-codepoint table, ``_romanize_word`` walks the syllables.
  2. The TRAILING inherent schwa is dropped (``ghara`` -> ``ghar``,
     ``dosta`` -> ``dost``): Hindi deletes it in speech and keeping it is the
     single thing that makes naive transliteration look wrong. Long ``ā`` is
     carried as the marker ``\x00`` until after that trim, so a genuine long
     vowel ending (``kyaa``) is never mistaken for a schwa.
  3. Long ``ī``/``ū`` fold to ``i``/``u`` (people type "ki", not "kii"), while
     ``ā`` doubles to ``aa`` ("aap", "kaam", "yaar").

Word-level, so caption timings survive: each timed token is romanised on its
own and its LEADING SPACE (the word-boundary convention the whole pipeline
relies on) is preserved.

No third-party dependency: the mapping is 60 lines of table and the rest of
indic-transliteration's machinery (IAST round-tripping, Sanskrit sandhi) buys
nothing here.
"""

import re
import unicodedata

# Any Devanagari codepoint — used to skip romanising pure-ASCII/number tokens.
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")

# Long 'ā' parked on a sentinel until the trailing-schwa trim has run.
_LONG_A = "\x00"

_INDEPENDENT_VOWELS = {
    "अ": "a", "आ": _LONG_A, "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
    "ऋ": "ri", "ॠ": "ri", "ऌ": "li", "ॡ": "li",
    "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
    "ऍ": "e", "ऎ": "e", "ऑ": "o", "ऒ": "o",
}

# Matras (combining vowel signs) replace a consonant's inherent 'a'.
_MATRAS = {
    "ा": _LONG_A, "ि": "i", "ी": "i", "ु": "u", "ू": "u",
    "ृ": "ri", "ॄ": "ri", "ॢ": "li", "ॣ": "li",
    "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
    "ॅ": "e", "ॆ": "e", "ॉ": "o", "ॊ": "o",
}

# Consonants WITHOUT their inherent vowel; the walker adds 'a' or a matra.
_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "ऩ": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ऱ": "r", "ल": "l", "ळ": "l", "ऴ": "l",
    "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # Precomposed nukta forms (Urdu/Persian sounds).
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y",
}

# A nukta after one of these makes a different sound; anything else keeps its
# base consonant (the nukta itself is then dropped).
_NUKTA = {
    "क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh",
    "फ": "f", "य": "y",
}

_SIGNS = {
    "ं": "n",   # anusvara      -> hain, main
    "ँ": "n",   # chandrabindu  -> hun
    "ः": "h",   # visarga
    "ऽ": "",    # avagraha
    "।": ".", "॥": ".",
}

_DIGITS = {c: str(i) for i, c in enumerate("०१२३४५६७८९")}

_VIRAMA = "्"
_NUKTA_SIGN = "़"


def _units(word):
    """Split a Devanagari token into syllable units.

    Each unit is ``{"cons", "vowel", "coda", "inherent"}``: the consonant
    cluster, its vowel, any nasal/visarga sign riding on it, and whether that
    vowel is the inherent schwa (the only one schwa deletion may remove).
    Non-syllabic characters (digits, punctuation, stray Latin) become units of
    their own so they survive untouched.
    """
    units = []
    i, n = 0, len(word)
    while i < n:
        ch = word[i]
        base = _CONSONANTS.get(ch)
        if base is not None:
            i += 1
            # क + ़  (decomposed nukta) is the same sound as the precomposed क़.
            if i < n and word[i] == _NUKTA_SIGN:
                base = _NUKTA.get(ch, base)
                i += 1
            unit = {"cons": base, "vowel": "a", "coda": "", "inherent": True}
            if i < n and word[i] == _VIRAMA:
                unit["vowel"], unit["inherent"] = "", False
                i += 1
            elif i < n and word[i] in _MATRAS:
                unit["vowel"], unit["inherent"] = _MATRAS[word[i]], False
                i += 1
            units.append(unit)
            continue
        i += 1
        if ch in _INDEPENDENT_VOWELS:
            units.append({"cons": "", "vowel": _INDEPENDENT_VOWELS[ch],
                          "coda": "", "inherent": False})
        elif ch in _MATRAS:  # orphan matra (malformed input)
            units.append({"cons": "", "vowel": _MATRAS[ch],
                          "coda": "", "inherent": False})
        elif ch in _SIGNS:
            if units and not units[-1]["coda"]:
                units[-1]["coda"] = _SIGNS[ch]
            else:
                units.append({"cons": _SIGNS[ch], "vowel": "",
                              "coda": "", "inherent": False})
        elif ch in _DIGITS:
            units.append({"cons": _DIGITS[ch], "vowel": "",
                          "coda": "", "inherent": False})
        elif ch not in (_VIRAMA, _NUKTA_SIGN):
            units.append({"cons": ch, "vowel": "", "coda": "",
                          "inherent": False})
    return units


def _delete_schwas(units):
    """Apply Hindi schwa deletion in place — ghar, not ghara; ladki, not ladaki.

    Two rules, in this order (the order is the algorithm: the final deletion
    is what stops the second rule from eating the schwa in samajh):

      1. The word-final inherent schwa always goes, in any word of more than
         one syllable. This is the single biggest source of "transliteration
         looks wrong": ghara, dosta, kapila.
      2. Then the standard VC_CV rule — an inherent schwa surrounded by a
         vowel before and a real vowel after is dropped (ladaki -> ladki,
         matalaba -> matlab), left to right so a schwa already deleted no
         longer supplies the vowel its neighbour would need. A CLOSED syllable
         before it blocks the rule: zindagi keeps its schwa, zindgi is not a
         word anyone types.
    """
    if len(units) > 1 and units[-1]["inherent"] and not units[-1]["coda"]:
        units[-1]["vowel"], units[-1]["inherent"] = "", False
    for i in range(1, len(units) - 1):
        if not units[i]["inherent"] or units[i]["coda"]:
            continue
        if (units[i - 1]["vowel"] and not units[i - 1]["coda"]
                and units[i + 1]["vowel"]):
            units[i]["vowel"], units[i]["inherent"] = "", False


def _romanize_word(word):
    """Romanise a single Devanagari token to readable Hinglish."""
    if not _DEVANAGARI.search(word):
        return word  # numbers / Latin / punctuation pass straight through

    units = _units(word)
    _delete_schwas(units)

    out = []
    for idx, unit in enumerate(units):
        vowel = unit["vowel"]
        # Long 'aa' doubles inside a word (aap, kaam, pyaar) but not at the
        # end, where nobody types the second one: sharma, kya, achcha.
        if vowel == _LONG_A:
            vowel = "a" if (idx == len(units) - 1 and len(units) > 1) else "aa"
        out.append(unit["cons"] + vowel + unit["coda"])

    roman = "".join(out).replace(_LONG_A, "aa")
    # च्छ is ch + chh; nobody writes the doubled h twice (achcha, not achchha).
    roman = roman.replace("chchh", "chch")

    # Fold any stray diacritic that came in with the source text.
    return "".join(
        c for c in unicodedata.normalize("NFKD", roman)
        if not unicodedata.combining(c)
    ).lower()


def to_roman(text):
    """Romanise a run of text token-by-token (whitespace preserved)."""
    if not text or not _DEVANAGARI.search(text):
        return text
    # Split on whitespace but KEEP the separators: a word token's leading
    # space is the pipeline's word-boundary marker.
    parts = re.split(r"(\s+)", text)
    return "".join(p if p.isspace() else _romanize_word(p) for p in parts)


def romanize_transcript(transcript):
    """Return a copy of a transcript with every text field romanised.

    Handles both transcript shapes in this repo: segments carrying their own
    ``words`` (transcribe_backends) and a flat top-level ``words`` list. Word
    timings are untouched, so captions stay in sync. ``language`` becomes
    ``"hinglish"`` so downstream prompts and metadata say what the text is.
    """
    if not transcript:
        return transcript

    out = dict(transcript)
    out["language"] = "hinglish"
    if transcript.get("text"):
        out["text"] = to_roman(transcript["text"])
    if isinstance(transcript.get("words"), list):
        out["words"] = [
            {**w, "word": to_roman(w.get("word", ""))} for w in transcript["words"]
        ]
    out["segments"] = [
        {
            **s,
            "text": to_roman(s.get("text", "")),
            **({"words": [{**w, "word": to_roman(w.get("word", ""))}
                          for w in s["words"]]}
               if isinstance(s.get("words"), list) else {}),
        }
        for s in (transcript.get("segments") or [])
    ]
    return out


def romanize_if_needed(transcript):
    """Romanise a transcript only when it actually carries Devanagari.

    For the reuse paths (a Thumbnail Studio handover, the checkpoint an
    interrupted run left behind), where the text may already be Latin — an
    English transcript must not be retagged as Hinglish.
    """
    text = (transcript or {}).get("text") or ""
    if not text:
        text = " ".join(str(s.get("text") or "")
                        for s in (transcript.get("segments") or []))
    if not _DEVANAGARI.search(text):
        return transcript
    return romanize_transcript(transcript)
