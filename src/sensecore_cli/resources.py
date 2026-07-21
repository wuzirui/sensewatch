"""Generic parsing helpers for SenseCore resource-spec payloads."""

from __future__ import annotations

from typing import Any


def gpu_count_from_spec(spec: dict[str, Any]) -> int:
    device = spec.get("device")
    if isinstance(device, dict):
        value = device.get("number")
        if value is not None:
            return int(value)
    limits = spec.get("limits") or {}
    value = limits.get("nvidia.com/gpu", 0) or limits.get("device", 0) or 0
    return int(value)


def _to_float_or_inf(value: Any) -> float:
    if value is None:
        return float("inf")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return float("inf")


def cpu_allocatable_from_spec(spec: dict[str, Any]) -> float:
    cpu = spec.get("cpu")
    if isinstance(cpu, dict):
        for key in ("vcpu_allocatable", "allocatable", "capacity"):
            if cpu.get(key) is not None:
                return _to_float_or_inf(cpu[key])
    for field in ("requests", "limits"):
        container = spec.get(field)
        if isinstance(container, dict) and container.get("cpu") is not None:
            return _to_float_or_inf(container["cpu"])
    return float("inf")


def memory_allocatable_from_spec(spec: dict[str, Any]) -> float:
    memory = spec.get("memory")
    if isinstance(memory, dict):
        for key in ("allocatable", "capacity"):
            if memory.get(key) is not None:
                return _to_float_or_inf(memory[key])
    for field in ("requests", "limits"):
        container = spec.get(field)
        if isinstance(container, dict) and container.get("memory") is not None:
            return _to_float_or_inf(container["memory"])
    return float("inf")
