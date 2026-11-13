from homeassistant.util import dt as dt_util


def get_datetime(date=None):
    """Datetime in the time zone configured in Home Assistant."""
    if date is None:
        return dt_util.now()
    return dt_util.as_local(date)


def to_int(value, default=None):
    """int() that never raises: returns default for None / invalid values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
