"""routed_via_gateway must never guess -- callers present its answer as fact."""

from omarchy_cast.core import net


def runner_returning(code, out):
    return lambda argv: (code, out)


def test_an_on_link_address_is_not_via_a_gateway():
    out = "10.10.10.231 dev wlan0 src 10.10.10.127 uid 1000 \n    cache"

    assert net.routed_via_gateway("10.10.10.231", runner_returning(0, out)) is False


def test_an_address_through_a_gateway_is_detected():
    """The real output from the day this was needed: laptop on 172.26.x,
    Apple TV on 10.10.10.x."""
    out = "10.10.10.231 via 172.26.0.1 dev wlan0 src 172.26.2.208 uid 1000 \n    cache"

    assert net.routed_via_gateway("10.10.10.231", runner_returning(0, out)) is True


def test_a_failed_lookup_is_unknown_not_false():
    assert net.routed_via_gateway("10.0.0.1", runner_returning(1, "")) is None


def test_empty_output_is_unknown():
    assert net.routed_via_gateway("10.0.0.1", runner_returning(0, "   ")) is None


def test_only_the_first_line_is_considered():
    """A 'via' in a later cache line must not be read as the route itself."""
    out = "10.0.0.5 dev wlan0 src 10.0.0.2 \n    cache via something-else"

    assert net.routed_via_gateway("10.0.0.5", runner_returning(0, out)) is False
