def test_output_language_is_requested_when_set(monkeypatch) -> None:
    from jezero.config import SummarizerConfig
    from jezero.summarize import Summarizer

    monkeypatch.setenv("TEST_KEY", "k")
    base = {"model": "m", "api_key_env": "TEST_KEY"}
    plain = Summarizer(SummarizerConfig(**base))._prompt("hello", None, None)
    assert "whatever the document's language" not in plain
    fixed = Summarizer(SummarizerConfig(**base, output_language="Spanish"))._prompt(
        "hi", None, None
    )
    assert "in Spanish, whatever the document's language" in fixed
