from app.extraction.hashing import compute_note_hash


def test_same_input_produces_same_hash():
    h1 = compute_note_hash("2026-08-20T10:00:00", "Evolução", "Clínica Médica", "texto normalizado")
    h2 = compute_note_hash("2026-08-20T10:00:00", "Evolução", "Clínica Médica", "texto normalizado")
    assert h1 == h2


def test_different_text_produces_different_hash():
    h1 = compute_note_hash("2026-08-20T10:00:00", "Evolução", "Clínica Médica", "texto A")
    h2 = compute_note_hash("2026-08-20T10:00:00", "Evolução", "Clínica Médica", "texto B")
    assert h1 != h2


def test_hash_is_sha256_hex():
    h = compute_note_hash("t", "s", "e", "texto")
    assert len(h) == 64
    int(h, 16)  # não deve levantar ValueError
