"""Tests for generic SenseCore resource-spec parsing."""

import math

from sensecore_cli.resources import (
    cpu_allocatable_from_spec,
    gpu_count_from_spec,
    memory_allocatable_from_spec,
)


def test_gpu_count_prefers_device_number_and_supports_limits_fallback() -> None:
    assert gpu_count_from_spec({"device": {"number": 8}}) == 8
    assert gpu_count_from_spec({"limits": {"nvidia.com/gpu": "4"}}) == 4


def test_cpu_allocatable_supports_structured_and_legacy_specs() -> None:
    assert cpu_allocatable_from_spec({"cpu": {"vcpu_allocatable": 64}}) == 64.0
    assert cpu_allocatable_from_spec({"requests": {"cpu": "32"}}) == 32.0


def test_memory_allocatable_supports_structured_and_legacy_specs() -> None:
    assert memory_allocatable_from_spec({"memory": {"allocatable": 1024}}) == 1024.0
    assert memory_allocatable_from_spec({"limits": {"memory": "832"}}) == 832.0
    assert math.isinf(memory_allocatable_from_spec({}))
