import subprocess
import sys
import textwrap

from omarchy_cast.core import singleton


def test_the_first_daemon_gets_the_lock(tmp_path):
    assert singleton.acquire(tmp_path / "daemon.lock", wait=0) is not None


def test_a_second_daemon_is_refused(tmp_path):
    """The bug this exists to prevent: a second daemon sweeps the virtual
    output of a live cast owned by the first, killing it mid-stream."""
    lock = tmp_path / "daemon.lock"
    first = singleton.acquire(lock, wait=0)

    assert first is not None
    assert singleton.acquire(lock, wait=0) is None


def test_the_lock_is_released_when_the_holder_exits(tmp_path):
    """Otherwise a daemon that crashed would lock out every future one."""
    lock = tmp_path / "daemon.lock"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(tmp_path.parent.parent)!r})
        from omarchy_cast.core import singleton
        assert singleton.acquire({str(lock)!r}, wait=0) is not None
    """)
    subprocess.run([sys.executable, "-c", script], check=True, cwd="/home/mrcode/workspace/omarchy-cast")

    # The child is gone, so the kernel dropped its flock.
    assert singleton.acquire(lock, wait=0) is not None


def test_a_lock_in_an_unwritable_place_does_not_block_startup(tmp_path):
    """Refusing to cast because a lock file could not be created would be a
    worse failure than the one being prevented."""
    unwritable = tmp_path / "no-such-dir" / "x" / "daemon.lock"
    (tmp_path / "no-such-dir").mkdir()
    (tmp_path / "no-such-dir").chmod(0o500)

    try:
        assert singleton.acquire(unwritable, wait=0) is not None
    finally:
        (tmp_path / "no-such-dir").chmod(0o700)


def test_it_waits_briefly_for_a_departing_daemon(tmp_path, monkeypatch):
    """The daemon exits after 30s idle. A client spawning a replacement in the
    window where the old one still holds the lock must not be refused outright
    -- it would leave the user with no daemon at all."""
    lock = tmp_path / "daemon.lock"
    held = singleton.acquire(lock, wait=0)
    assert held is not None

    slept = []

    def fake_sleep(seconds):
        slept.append(seconds)
        # Model the holder exiting partway through the wait.
        if len(slept) == 2:
            held.close()

    monkeypatch.setattr(singleton.time, "sleep", fake_sleep)

    assert singleton.acquire(lock, wait=5.0) is not None
    assert slept, "should have waited rather than giving up immediately"
