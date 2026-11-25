import base64
import os
import stat
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import load_settings  # noqa: E402

COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".instagram.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc123\n"
)


def _env(monkeypatch, **kw):
    monkeypatch.setenv("BOT_TOKEN", "123:test")
    monkeypatch.delenv("COOKIES_FILE", raising=False)
    monkeypatch.delenv("COOKIES_B64", raising=False)
    for k, v in kw.items():
        monkeypatch.setenv(k, v)


def test_cookies_from_env_var(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    b64 = base64.b64encode(COOKIES.encode()).decode()
    _env(monkeypatch, COOKIES_B64=b64[:40] + "\n" + b64[40:])  # tolerates line breaks
    s = load_settings()
    assert s.cookies_file and open(s.cookies_file).read() == COOKIES
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(s.cookies_file).st_mode) == 0o600  # private file


def test_bad_cookie_values_are_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    _env(monkeypatch, COOKIES_B64="this is not base64 !!!")
    assert load_settings().cookies_file is None
    _env(monkeypatch, COOKIES_B64=base64.b64encode(b"just some text").decode())
    assert load_settings().cookies_file is None


def test_cookie_file_wins_over_env_var(monkeypatch, tmp_path):
    f = tmp_path / "cookies.txt"
    f.write_text(COOKIES)
    _env(monkeypatch, COOKIES_FILE=str(f), COOKIES_B64=base64.b64encode(b"x\ty").decode())
    assert load_settings().cookies_file == str(f)


def test_ask_format_defaults_on_and_can_be_turned_off(monkeypatch):
    from app.config import load_settings

    monkeypatch.setenv("BOT_TOKEN", "1:x")
    monkeypatch.delenv("ASK_FORMAT", raising=False)
    assert load_settings().ask_format is True
    monkeypatch.setenv("ASK_FORMAT", "0")
    assert load_settings().ask_format is False
