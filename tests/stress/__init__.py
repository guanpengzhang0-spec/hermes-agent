"""Stress / load tests for the orchestration borrow.

These are not unit tests — they exercise scale, concurrency, and
adversarial inputs to validate the upper-bound guarantees claimed in
docs/automaton-migration-changelog.md.

Each test asserts a generous upper bound so it stays green on slow
hardware (CI, WSL2). When something regresses by 10x the bound trips
and we know to look.

Run with::

    pytest tests/stress/ -v --no-header
"""
