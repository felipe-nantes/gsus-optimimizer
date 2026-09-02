"""Benchmark único e manual do LLM local com binário/modelo reais -- mede
tempo de carregamento e de uma análise, usando SÓ dado sintético
(fixtures/notes/awaiting_exam.txt, prontuário fictício). Não é parte da
suíte de testes automatizada (depende de hardware/tempo, não de corretude)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis.llm import LocalLLM
from app.analysis.rules import StructuredNote

FIXTURE = (Path(__file__).resolve().parent.parent / "fixtures" / "notes" / "awaiting_exam.txt").read_text(encoding="utf-8")

notes = [
    StructuredNote(timestamp="2026-08-20T08:00:00", specialty="Clínica Médica", source_type="Evolução",
                    text="Paciente Teste 001, prontuario 123456, leito 2A. Internado para investigacao de dor abdominal. Solicitada tomografia de abdome."),
    StructuredNote(timestamp="2026-08-20T14:00:00", specialty="Clínica Médica", source_type="Evolução",
                    text="Paciente estavel, aguardando realizacao da tomografia de abdome solicitada."),
]

llm = LocalLLM(
    model_path=Path("models/model.gguf"),
    server_path=Path("runtime/llama-server.exe"),
    timeout_seconds=180,
    max_retries=2,
    startup_timeout_seconds=300,
)

print("Iniciando llama-server (carregando modelo, pode demorar em CPU fraca)...")
t0 = time.monotonic()
try:
    llm.start()
    t1 = time.monotonic()
    print(f"Servidor pronto em {t1 - t0:.1f}s")

    print("Rodando 1 análise (sintética, prontuário fictício)...")
    t2 = time.monotonic()
    result = llm.analyze_patient(previous_state=None, new_notes=notes)
    t3 = time.monotonic()
    print(f"Análise concluída em {t3 - t2:.1f}s")
    print("insufficient_information:", result.get("insufficient_information"))
    print("pending_items:", len(result.get("pending_items", [])))
    print("clinical_context (primeiros 200 chars):", (result.get("clinical_context") or "")[:200])
finally:
    llm.stop()
    print(f"Tempo total (carga + 1 análise): {time.monotonic() - t0:.1f}s")
