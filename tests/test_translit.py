# -*- coding: utf-8 -*-
"""Hinglish romanisation: Devanagari speech written in Latin letters."""

import translit


def test_common_words_read_the_way_people_type_them():
    cases = {
        "आप": "aap",            # long aa doubles inside a word
        "कैसे": "kaise",
        "हैं": "hain",          # anusvara -> n
        "क्या": "kya",          # final long aa does NOT double
        "नहीं": "nahin",
        "बहुत": "bahut",
        "दिल": "dil",
        "प्यार": "pyaar",
        "यार": "yaar",
        "सच": "sach",
        "बोलो": "bolo",
    }
    for source, expected in cases.items():
        assert translit.to_roman(source) == expected


def test_trailing_schwa_is_deleted():
    # The #1 thing that makes naive transliteration look wrong: "ghara".
    assert translit.to_roman("घर") == "ghar"
    assert translit.to_roman("दोस्त") == "dost"
    assert translit.to_roman("कपिल") == "kapil"


def test_medial_schwa_deletion_and_its_two_blockers():
    assert translit.to_roman("लड़की") == "ladki"      # VC_CV -> dropped
    assert translit.to_roman("मतलब") == "matlab"
    assert translit.to_roman("नमकीन") == "namkin"
    # No vowel after it (the final schwa went first), so it stays.
    assert translit.to_roman("समझ") == "samajh"
    # Closed syllable before it: zindgi is not a word anyone types.
    assert translit.to_roman("ज़िंदगी") == "zindagi"


def test_nukta_forms_are_the_urdu_sounds():
    assert translit.to_roman("मज़ाक") == "mazaak"
    assert translit.to_roman("बड़ा") == "bada"
    assert translit.to_roman("पढ़ाई") == "padhaai"
    # Decomposed nukta (क + ़) must romanise like the precomposed codepoint.
    assert translit.to_roman("क़") == translit.to_roman("क़")


def test_non_devanagari_text_is_returned_unchanged():
    assert translit.to_roman("OpenShorts 2026!") == "OpenShorts 2026!"
    assert translit.to_roman("") == ""
    assert translit.to_roman(None) is None


def test_leading_space_word_boundary_survives():
    # The whole pipeline marks a word start with a leading space; losing it
    # glues caption words together.
    assert translit.to_roman(" आप") == " aap"
    assert translit.to_roman(" आप कैसे हैं") == " aap kaise hain"


def test_romanize_transcript_keeps_timings_and_retags_the_language():
    transcript = {
        "language": "hi",
        "text": "आप कैसे हैं",
        "segments": [{
            "start": 1.0, "end": 2.5, "text": "आप कैसे हैं",
            "words": [
                {"word": " आप", "start": 1.0, "end": 1.4},
                {"word": " कैसे", "start": 1.4, "end": 1.9},
                {"word": " हैं", "start": 1.9, "end": 2.5},
            ],
        }],
    }
    out = translit.romanize_transcript(transcript)

    assert out["language"] == "hinglish"
    assert out["text"] == "aap kaise hain"
    assert out["segments"][0]["text"] == "aap kaise hain"
    assert [w["word"] for w in out["segments"][0]["words"]] == [
        " aap", " kaise", " hain"]
    assert [(w["start"], w["end"]) for w in out["segments"][0]["words"]] == [
        (1.0, 1.4), (1.4, 1.9), (1.9, 2.5)]
    # The input is not mutated — callers keep the native-script transcript.
    assert transcript["segments"][0]["text"] == "आप कैसे हैं"
    assert transcript["language"] == "hi"


def test_romanize_transcript_handles_a_flat_word_list():
    out = translit.romanize_transcript({
        "language": "hi", "text": "घर",
        "words": [{"word": " घर", "start": 0.0, "end": 0.5}],
        "segments": [],
    })
    assert out["words"] == [{"word": " ghar", "start": 0.0, "end": 0.5}]
