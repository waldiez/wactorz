"""Guessing the key has to get expensive, and stay cheap for a typo.

One shared key and no accounts means guessing is the whole attack surface of
the sign-in form. A per-attempt `sleep` does not answer it — requests fired in
parallel all sleep at the same time — so attempts are counted and refused
instead, which holds however many are in flight.

Time is injected rather than slept, so the backoff is asserted exactly.
"""

from wactorz.web.throttle import (
    BACKOFF_BASE_SECONDS,
    FORGET_AFTER_SECONDS,
    LOCKOUT_AFTER,
    LOCKOUT_SECONDS,
    LoginThrottle,
)

IP = "192.168.1.50"


class TestAnAddressThatHasNotFailed:
    def test_may_try_immediately(self) -> None:
        assert LoginThrottle().retry_after(IP) == 0.0

    def test_one_address_failing_does_not_delay_another(self) -> None:
        # The counter is per address, or one guesser would lock out the house.
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)

        assert throttle.retry_after("10.0.0.9", now=0.0) == 0.0


class TestTheBackoff:
    def test_a_first_mistake_costs_about_a_second(self) -> None:
        # A typo should be an annoyance, not a punishment.
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)

        assert throttle.retry_after(IP, now=0.0) == BACKOFF_BASE_SECONDS

    def test_each_failure_doubles_the_wait(self) -> None:
        throttle = LoginThrottle()
        waits = []
        for attempt in range(3):
            throttle.record_failure(IP, now=float(attempt))
            waits.append(throttle.retry_after(IP, now=float(attempt)))

        assert waits == [1.0, 2.0, 4.0]

    def test_the_wait_elapses(self) -> None:
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)

        assert throttle.retry_after(IP, now=BACKOFF_BASE_SECONDS) == 0.0

    def test_waiting_it_out_does_not_reset_the_count(self) -> None:
        # Otherwise the backoff is defeated by simply being patient between
        # attempts, which is exactly what a script can afford to be.
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)
        throttle.record_failure(IP, now=100.0)

        assert throttle.retry_after(IP, now=100.0) == 2.0

    def test_pruning_between_attempts_does_not_reset_it_either(self) -> None:
        # The same property, asserted against the sequence the handler actually
        # performs: it prunes on every submit, so a store that forgot an entry
        # as soon as its wait elapsed would give a patient script a clean record
        # before each guess — and the lockout would catch only the impatient.
        throttle = LoginThrottle()
        for attempt in range(4):
            throttle.prune(now=attempt * 60.0)
            throttle.record_failure(IP, now=attempt * 60.0)

        throttle.prune(now=240.0)
        throttle.record_failure(IP, now=240.0)

        assert throttle.retry_after(IP, now=240.0) == LOCKOUT_SECONDS


class TestTheLockout:
    def test_it_closes_after_the_fifth_failure(self) -> None:
        throttle = LoginThrottle()
        for attempt in range(LOCKOUT_AFTER):
            throttle.record_failure(IP, now=0.0)

        assert throttle.retry_after(IP, now=0.0) == LOCKOUT_SECONDS

    def test_it_stays_closed_for_the_whole_window(self) -> None:
        throttle = LoginThrottle()
        for _ in range(LOCKOUT_AFTER):
            throttle.record_failure(IP, now=0.0)

        assert throttle.retry_after(IP, now=LOCKOUT_SECONDS - 1) == 1.0
        assert throttle.retry_after(IP, now=LOCKOUT_SECONDS) == 0.0

    def test_failing_again_while_locked_out_extends_it(self) -> None:
        throttle = LoginThrottle()
        for _ in range(LOCKOUT_AFTER):
            throttle.record_failure(IP, now=0.0)

        throttle.record_failure(IP, now=60.0)

        assert throttle.retry_after(IP, now=60.0) == LOCKOUT_SECONDS

    def test_the_window_is_a_quarter_of_an_hour(self) -> None:
        assert LOCKOUT_SECONDS == 15 * 60


class TestSigningInSuccessfully:
    def test_it_forgets_the_failures(self) -> None:
        # Proof of not guessing. Keeping the count would make the next typo
        # cost more than a first one.
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)
        throttle.record_failure(IP, now=0.0)

        throttle.record_success(IP)

        assert throttle.retry_after(IP, now=0.0) == 0.0

    def test_it_leaves_other_addresses_alone(self) -> None:
        throttle = LoginThrottle()
        throttle.record_failure("10.0.0.9", now=0.0)

        throttle.record_success(IP)

        assert throttle.retry_after("10.0.0.9", now=0.0) > 0.0


class TestTheStoreDoesNotGrowForever:
    def test_addresses_quiet_for_long_enough_are_forgotten(self) -> None:
        # A guesser walking a subnet would otherwise leave one entry per
        # address for as long as the process runs.
        throttle = LoginThrottle()
        for host in range(50):
            throttle.record_failure(f"10.0.0.{host}", now=0.0)

        throttle.prune(now=FORGET_AFTER_SECONDS)

        assert len(throttle) == 0

    def test_an_address_is_kept_while_its_failures_still_count(self) -> None:
        # Its wait has long elapsed; what keeps it is the count, which is what
        # a later attempt has to add to.
        throttle = LoginThrottle()
        throttle.record_failure(IP, now=0.0)

        throttle.prune(now=FORGET_AFTER_SECONDS - 1)

        assert len(throttle) == 1

    def test_an_address_still_waiting_is_kept(self) -> None:
        throttle = LoginThrottle()
        for _ in range(LOCKOUT_AFTER):
            throttle.record_failure(IP, now=0.0)

        throttle.prune(now=60.0)

        assert throttle.retry_after(IP, now=60.0) > 0.0
