import config
from config import ADMIN_API_KEY, APP_TIMEZONE, DATA_ROOT, TOKEN_DIR, USERS_DB_DIR


def test_config_paths_under_test_data():
    assert DATA_ROOT.name == ".test-data"
    assert USERS_DB_DIR.name == "users"
    assert TOKEN_DIR is not None


def test_timezone_europe_madrid():
    assert APP_TIMEZONE.key == "Europe/Madrid"


def test_admin_api_key_is_env_driven():
    assert isinstance(ADMIN_API_KEY, str)


def test_data_dir_exists():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    assert DATA_ROOT.exists()


def test_config_exposes_dir_helpers():
    assert hasattr(config, "_resolve_token_dir")
    assert callable(config._resolve_token_dir)
