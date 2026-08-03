import os

from dotenv import load_dotenv

CONFIG_DIR = os.path.dirname(__file__)

# Loads backend/config/.env for local dev (gitignored). No-op if the file
# doesn't exist, e.g. on Render, where these are set as real env vars instead.
load_dotenv(os.path.join(CONFIG_DIR, ".env"))

DB_CONFIG = {
    "host": os.environ["DB_HOST"],
    "port": int(os.environ["DB_PORT"]),
    "user": os.environ["DB_USER"],
    "password": os.environ["DB_PASSWORD"],
    "database": os.environ["DB_NAME"],
    # Aiven requires SSL. Locally this points at a CA cert downloaded from the
    # service's Overview page in the Aiven Console; on Render it points at the
    # path of a mounted Secret File (see render.yaml / README). A relative
    # path is resolved against this config directory, not the process's
    # working directory, so it works regardless of where the app is launched
    # from; Render's Secret File path is already absolute.
    # "ssl_ca": os.path.join(CONFIG_DIR, os.environ["DB_SSL_CA_PATH"]),
    # "ssl_verify_cert": True,
    # The C extension (_mysql_connector) calls SSL_CTX_set_default_verify_paths()
    # unconditionally, which fails on macOS since it lacks the Linux-style default
    # cert store paths. use_pure avoids that call and uses ssl_ca directly.
    # "use_pure": True,
    "ssl_disabled": True,
}
