"""Tests for the SHIPPED geofence.toml, not a fixture.

Every other test builds its own config, which means the file you actually
deploy is the one thing nothing checks. A typo'd region id in a commitment
loads fine right up until 5:30am, when the alert that doesn't fire is the only
symptom. These run in CI on every push, so that typo fails a build instead.
"""

from pathlib import Path

import pytest

from geofence.config import _DAY_IDS, _parse_hhmm, load_config

CONFIG_PATH = Path(__file__).resolve().parent.parent / "geofence.toml"


@pytest.fixture(scope="module")
def shipped():
    return load_config(str(CONFIG_PATH))


def test_the_shipped_config_loads(shipped):
    # load_config validates regions, days and times, so merely getting here
    # proves a lot. The explicit checks below cover what it can't.
    assert shipped.commitments, "no commitments configured"


def test_every_commitment_points_at_real_regions(shipped):
    for c in shipped.commitments:
        assert c.origin in shipped.regions
        assert c.destination in shipped.regions
        assert c.origin != c.destination, f"{c.id} starts where it ends"
        _parse_hhmm(c.arrive_by)
        assert all(d in _DAY_IDS for d in c.days)


def test_the_two_commitments_you_asked_for_are_present(shipped):
    by_id = {c.id: c for c in shipped.commitments}
    swim = by_id["swim_practice"]
    assert (swim.origin, swim.destination) == ("henry_hall", "denunzio")
    lunch = by_id["club_lunch"]
    assert (lunch.origin, lunch.destination) == ("henry_hall", "prospect_club")


def test_prep_time_is_a_useful_amount_of_warning(shipped):
    """A ping 30 seconds before you must leave is not a warning."""
    for c in shipped.commitments:
        assert c.prep_sec >= 300, f"{c.id}: prep_sec is too short to act on"
        assert c.safety_margin_sec >= 0


def test_coffee_window_is_coherent(shipped):
    assert shipped.coffee.region in shipped.regions
    assert _parse_hhmm(shipped.coffee.earliest) < _parse_hhmm(shipped.coffee.latest)


def test_digest_schedule_is_valid(shipped):
    assert shipped.digest.day in _DAY_IDS
    _parse_hhmm(shipped.digest.time)


def test_regions_have_readable_names(shipped):
    """Messages say "DeNunzio Pool", not "denunzio"."""
    for rid in ("henry_hall", "denunzio", "prospect_club"):
        assert shipped.region_name(rid) != rid


def _walk(node, path=()):
    """Yield (dotted-key, value) for every scalar in a parsed toml tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, path + (str(i),))
    else:
        yield ".".join(path), node


def test_no_secrets_are_committed_in_the_config():
    """Tokens belong in geofence.env (gitignored), never in the toml.

    Checked against the PARSED file, not the raw text: the comments legitimately
    name these variables to explain where they do live, and a substring scan
    would flag that documentation forever.
    """
    import tomllib

    with open(CONFIG_PATH, "rb") as f:
        data = tomllib.load(f)

    forbidden = {"bot_token", "chat_id", "secret", "token", "password"}
    for key, value in _walk(data):
        leaf = key.rsplit(".", 1)[-1].lower()
        assert leaf not in forbidden, f"{key} must not be set in geofence.toml"
        # A Telegram bot token looks like "123456789:AAH...". Catch a pasted one
        # under any key name at all.
        if isinstance(value, str) and ":" in value:
            head, _, tail = value.partition(":")
            assert not (
                head.isdigit() and len(head) >= 6 and len(tail) > 20
            ), f"{key} looks like a pasted bot token"
