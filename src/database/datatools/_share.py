TIMESTAMP = r"%Y%m%d%H%M%S"


class _UnsetType:
    """Sentinel indicating a parameter was not provided."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "UNSET"

    def __bool__(self):
        return False


UNSET = _UnsetType()
