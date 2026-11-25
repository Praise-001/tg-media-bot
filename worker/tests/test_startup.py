"""The bot must say why it can't start, not exit silently."""
import os
import subprocess
import sys

WORKER_DIR = os.path.dirname(os.path.dirname(__file__))


def _run(tmp_path, **env_overrides):
    env = dict(os.environ)
    env["PYTHONPATH"] = WORKER_DIR
    env.update(env_overrides)  # set (even to "") rather than delete, so a local .env can't override
    return subprocess.run(
        [sys.executable, "-m", "app.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )


def test_missing_token_is_reported(tmp_path):
    r = _run(tmp_path, BOT_TOKEN="")
    assert r.returncode != 0
    assert "BOT_TOKEN is missing" in r.stderr


def test_bad_admin_id_is_reported(tmp_path):
    r = _run(tmp_path, BOT_TOKEN="1:x", ADMIN_IDS="@myname")
    assert r.returncode != 0
    assert "ADMIN_IDS must be numeric" in r.stderr
