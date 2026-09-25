from quizpilot.config import load_config


def test_deepseek_model_name_is_normalized(tmp_path, monkeypatch):
    monkeypatch.delenv("QUIZPILOT_MODEL", raising=False)
    cfg_file = tmp_path / "quizpilot.toml"
    cfg_file.write_text('[llm]\nmodel = " deepseek-Flash "\n', encoding="utf-8")
    assert load_config(cfg_file).llm.model == "deepseek-flash"


def test_other_providers_keep_case(tmp_path, monkeypatch):
    monkeypatch.delenv("QUIZPILOT_MODEL", raising=False)
    monkeypatch.delenv("QUIZPILOT_BASE_URL", raising=False)
    cfg_file = tmp_path / "quizpilot.toml"
    cfg_file.write_text('[llm]\nbase_url = "https://example.com/v1"\nmodel = "Qwen-Max"\n', encoding="utf-8")
    assert load_config(cfg_file).llm.model == "Qwen-Max"
