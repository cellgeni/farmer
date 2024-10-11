import os


def is_dev_mode() -> bool:
    return get_dev_user() is not None


def get_dev_user() -> str | None:
    return os.environ.get("FARMER_DEV_USER")
