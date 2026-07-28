"""P16.4 compatibility exports for the P16.5 REST implementation."""

from app.services.cuopt_rest import (
    CuOptManagedError,
    CuOptManagedOptimizer,
    CuOptRestError,
    CuOptRestOptimizer,
    ManagedCuOptResult,
    CuOptRestResult,
    build_cuopt_routing_payload,
    build_managed_routing_payload,
)

__all__ = [
    "CuOptManagedError",
    "CuOptManagedOptimizer",
    "CuOptRestError",
    "CuOptRestOptimizer",
    "ManagedCuOptResult",
    "CuOptRestResult",
    "build_cuopt_routing_payload",
    "build_managed_routing_payload",
]
