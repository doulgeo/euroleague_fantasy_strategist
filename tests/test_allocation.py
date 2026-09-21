"""Tests for proj/allocation.py - spec §6's required allocation coverage:
sums to total within tolerance, respects caps, infeasibility raises,
monotonicity in a player's own raw minutes, and removing a player raises
his position-group teammates' minutes.
"""

from __future__ import annotations

import numpy as np
import pytest

from proj.allocation import allocate_minutes, minutes_given_available


def test_allocation_sums_to_total_and_respects_cap():
    raw = np.array([38.0, 30.0, 25.0, 20.0, 15.0, 12.0, 10.0, 8.0, 5.0, 3.0])
    cap = 32.0
    total = 200.0
    m = allocate_minutes(raw, cap, total)
    assert m.sum() == pytest.approx(total, abs=1e-6)
    assert np.all(m <= cap + 1e-9)


def test_allocation_respects_per_player_cap_array():
    raw = np.array([40.0, 35.0, 30.0, 25.0, 20.0])
    cap = np.array([32.0, 34.0, 30.0, 34.0, 34.0])
    total = 150.0
    m = allocate_minutes(raw, cap, total)
    assert m.sum() == pytest.approx(total, abs=1e-6)
    assert np.all(m <= cap + 1e-9)


def test_allocation_infeasible_raises():
    raw = np.array([10.0, 10.0, 10.0])
    cap = 20.0
    total = 100.0  # cap * n = 60 < 100
    with pytest.raises(ValueError):
        allocate_minutes(raw, cap, total)


def test_allocation_monotonic_in_own_raw_minutes():
    """Raising one player's raw minutes, holding everyone else's raw
    fixed, never lowers his own allocated minutes."""
    others = [30.0, 25.0, 20.0, 15.0, 10.0, 8.0, 6.0, 4.0, 2.0]
    cap = 32.0
    total = 200.0
    raw_values = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0]
    allocated = []
    for r in raw_values:
        m = allocate_minutes([r] + others, cap, total)
        allocated.append(m[0])
    assert all(b >= a - 1e-9 for a, b in zip(allocated, allocated[1:]))


def test_removing_a_player_raises_position_group_teammates_minutes():
    """Removing a starter (via minutes_given_available's availability
    mask) raises the allocated minutes of remaining players - checked
    specifically for teammates who share his position group, per spec
    §6."""
    # index: 0=Guard, 1=Center, 2=Center, 3=Guard
    raw = np.array([15.0, 15.0, 10.0, 10.0])
    pos_groups = np.array(["G", "C", "C", "G"])
    cap = 32.0
    total = 30.0

    all_available = np.array([True, True, True, True])
    baseline = minutes_given_available(raw, all_available, cap, total)

    center_removed = np.array([True, False, True, True])  # remove player 1, a Center
    after_removal = minutes_given_available(raw, center_removed, cap, total)

    # player 2 is the removed player's position-group teammate (Center)
    assert after_removal[2] > baseline[2]
    assert after_removal[1] == 0.0  # the removed player himself gets 0
