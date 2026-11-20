"""Small shared constants used by the server and by static-file serving.

Also loads settings from a `.env` file in the project folder (if there is
one) into the environment, so API keys and bot tokens can be configured by
editing one file instead of the hosting panel. Real environment variables
win over the file. `.env` is git-ignored; `.env.example` shows the keys.
"""
import os

PORT = 5000
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path=os.path.join(BASE, '.env')):
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file()
