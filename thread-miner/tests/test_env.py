"""load_env precedence and robustness against hand-edited .env files."""

from thread_miner import load_env


def test_env_file_wins_over_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-stale-machine-key")
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-fresh-project-key\n")
    assert load_env(env_file)["ANTHROPIC_API_KEY"] == "sk-ant-fresh-project-key"


def test_process_env_used_when_file_lacks_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-machine")
    env_file = tmp_path / ".env"
    env_file.write_text("REDDIT_CLIENT_ID=abc\n")
    values = load_env(env_file)
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-from-machine"
    assert values["REDDIT_CLIENT_ID"] == "abc"


def test_quotes_bom_and_blanks_tolerated(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-machine-fallback")
    env_file = tmp_path / ".env"
    # BOM at file start (Notepad), quoted value, comment, and an empty value
    env_file.write_bytes(
        b'\xef\xbb\xbfREDDIT_CLIENT_ID="abc123"\n'
        b"# comment line\n"
        b"REDDIT_CLIENT_SECRET='s3cret'\n"
        b"ANTHROPIC_API_KEY=\n"
    )
    values = load_env(env_file)
    assert values["REDDIT_CLIENT_ID"] == "abc123"
    assert values["REDDIT_CLIENT_SECRET"] == "s3cret"
    # empty value in the file must not clobber a usable machine key
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-machine-fallback"
