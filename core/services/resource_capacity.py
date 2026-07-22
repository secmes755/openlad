"""
Resource Capacity Manager (External Mode)
Calculates max tenants based on memory and disk; no local GPU detection.
LLM is deployed externally; OpenLAD only provides deployment advice such as context length.
"""

import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Optional, Dict, Union

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class ResourceSnapshot:
    """Resource snapshot (External mode: no GPU)"""
    mem_total_mb: int
    mem_available_mb: int
    disk_total_gb: int
    disk_free_gb: int
    llm_context_size: int
    avg_query_chars: int


@dataclass
class CapacityPlan:
    """Capacity planning result (External mode)"""
    max_tenants: int                     # Max registered tenants (hard limit)
    max_concurrent_queries: int          # Max concurrent queries (env var controlled)
    recommended_active_users: int        # Recommended concurrent online users (quality of experience)
    per_user_query_per_minute: int       # Per-user query quota per minute (dynamically computed)
    per_user_upload_per_minute: int      # Per-user upload quota per minute
    memory_limit_reason: str             # Memory limit explanation
    disk_limit_reason: str               # Disk limit explanation
    deployment_advice: str               # Deployment advice (context length, etc.)


class ResourceCapacityManager:
    """Resource Capacity Manager

    Singleton; initialized at system startup. Other modules access via get_capacity_manager().
    """

    def __init__(self):
        self._snapshot: Optional[ResourceSnapshot] = None
        self._plan: Optional[CapacityPlan] = None
        self._last_compute_time: float = 0.0
        self._compute_interval_sec = float('inf')  # Hardware doesn't change dynamically; compute once at startup

    # =========================================================================
    # Resource Detection (memory + disk, no GPU detection)
    # =========================================================================

    def _detect_memory(self) -> Dict:
        """Detect system memory, returns {total_mb, available_mb}. Cross-platform via psutil."""
        try:
            import psutil
            mem = psutil.virtual_memory()
            return {
                "total_mb": mem.total // (1024 * 1024),
                "available_mb": mem.available // (1024 * 1024),
            }
        except ImportError:
            # Fallback to /proc/meminfo on Linux if psutil not installed
            try:
                with open("/proc/meminfo", "r") as f:
                    meminfo = f.read()
                total_kb = 0
                available_kb = 0
                for line in meminfo.split("\n"):
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        available_kb = int(line.split()[1])
                return {
                    "total_mb": total_kb // 1024,
                    "available_mb": available_kb // 1024,
                }
            except Exception as e:
                logger.warning(f"[CAPACITY] Memory detection failed: {e}")
            return {"total_mb": 0, "available_mb": 0}

    def _detect_disk(self) -> Dict:
        """Detect disk space, returns {total_gb, free_gb}"""
        try:
            stat = shutil.disk_usage(str(settings.DATA_DIR))
            return {
                "total_gb": stat.total // (1024 ** 3),
                "free_gb": stat.free // (1024 ** 3),
            }
        except Exception as e:
            logger.warning(f"[CAPACITY] Disk detection failed: {e}")
        return {"total_gb": 0, "free_gb": 0}

    # =========================================================================
    # Capacity Calculation Model
    # =========================================================================

    def _compute_capacity(self) -> CapacityPlan:
        """Compute system capacity plan (External mode: based on memory and disk, no GPU detection)"""
        mem = self._detect_memory()
        disk = self._detect_disk()

        # Read context window size from env var
        llm_ctx = int(os.environ.get("OPENLAD_LLM_CONTEXT_SIZE", "131072"))
        avg_chars = settings.CONTEXT_CONFIG.get("standard_chars", 60000)
        max_concurrent = int(os.environ.get("OPENLAD_QUERY_MAX_CONCURRENT", "1"))

        self._snapshot = ResourceSnapshot(
            mem_total_mb=mem["total_mb"],
            mem_available_mb=mem["available_mb"],
            disk_total_gb=disk["total_gb"],
            disk_free_gb=disk["free_gb"],
            llm_context_size=llm_ctx,
            avg_query_chars=avg_chars,
        )

        # ------------------------------------------------------------------
        # 1. Memory limit
        # ------------------------------------------------------------------
        system_reserve_mb = 4096  # 4GB reserved for system
        usable_mem_mb = max(0, mem["available_mb"] - system_reserve_mb)
        mem_per_active_tenant_mb = 800  # Including context cache, SQLite, peak reservation
        mem_based_users = max(1, usable_mem_mb // mem_per_active_tenant_mb)

        memory_reason = (f"Available memory {usable_mem_mb}MB (after system reserve {system_reserve_mb}MB), "
                         f"~{mem_per_active_tenant_mb}MB per active tenant")

        # ------------------------------------------------------------------
        # 2. Disk limit
        # ------------------------------------------------------------------
        disk_reserve_gb = 50
        usable_disk_gb = max(0, disk["free_gb"] - disk_reserve_gb)
        disk_per_tenant_gb = 1  # Conservative estimate 1GB/tenant
        disk_based_users = max(1, usable_disk_gb // disk_per_tenant_gb)

        disk_reason = f"Available disk {usable_disk_gb}GB (after reserve {disk_reserve_gb}GB), ~{disk_per_tenant_gb}GB per tenant"

        # ------------------------------------------------------------------
        # 3. Combined: take the strictest (memory + disk only)
        # ------------------------------------------------------------------
        max_tenants = min(mem_based_users, disk_based_users)
        recommended_active = max(1, int(max_tenants * 0.8))

        # Deployment advice
        deployment_advice = (
            f"External mode — LLM deployed externally. Recommended context window >= {llm_ctx} tokens, "
            f"~{avg_chars} chars per request. Concurrency controlled by OPENLAD_QUERY_MAX_CONCURRENT (current={max_concurrent})."
        )

        # Dynamic rate limiting: fewer max tenants, higher per-user quota
        if max_tenants <= 3:
            qpm = 15  # Few tenants, generous per-user quota
        elif max_tenants <= 10:
            qpm = 10
        elif max_tenants <= 20:
            qpm = 6
        else:
            qpm = 4

        plan = CapacityPlan(
            max_tenants=max_tenants,
            max_concurrent_queries=max_concurrent,
            recommended_active_users=recommended_active,
            per_user_query_per_minute=qpm,
            per_user_upload_per_minute=max(2, qpm // 3),
            memory_limit_reason=memory_reason,
            disk_limit_reason=disk_reason,
            deployment_advice=deployment_advice,
        )

        logger.info(
            f"[CAPACITY] Capacity plan: max_tenants={plan.max_tenants}, "
            f"max_concurrent={plan.max_concurrent_queries}, "
            f"recommended_active={plan.recommended_active_users}, "
            f"qpm={plan.per_user_query_per_minute}"
        )
        logger.info(f"[CAPACITY] Deployment: {deployment_advice}")
        logger.info(f"[CAPACITY] Memory: {memory_reason}")
        logger.info(f"[CAPACITY] Disk: {disk_reason}")

        return plan

    # =========================================================================
    # Public Interface
    # =========================================================================

    def get_plan(self, force_refresh: bool = False) -> CapacityPlan:
        """Get capacity plan, recompute if needed"""
        now = time.time()
        if self._plan is None or force_refresh or (now - self._last_compute_time) > self._compute_interval_sec:
            self._plan = self._compute_capacity()
            self._last_compute_time = now
        return self._plan

    def get_snapshot(self) -> Optional[ResourceSnapshot]:
        """Get latest resource snapshot"""
        return self._snapshot

    def can_create_tenant(self, current_tenant_count: int) -> tuple:
        """Check whether a new tenant can be created

        Returns:
            (bool, str): (allowed, reason)
        """
        plan = self.get_plan()
        if current_tenant_count >= plan.max_tenants:
            return False, (
                f"System has reached maximum tenant limit ({plan.max_tenants}). "
                f"Current config (memory {self._snapshot.mem_available_mb}MB, disk {self._snapshot.disk_free_gb}GB) cannot support more tenants."
            )
        # Soft warning: alert when reaching 90%
        if current_tenant_count >= int(plan.max_tenants * 0.9):
            return True, (
                f"Warning: {current_tenant_count}/{plan.max_tenants} tenants created, "
                f"approaching system capacity limit."
            )
        return True, ""

    def get_query_concurrency_config(self) -> Dict:
        """Get query concurrency strategy config (External mode: env-var based)

        Returns:
            {
                "mode": "serial" | "semaphore",
                "max_concurrent": int,
                "reason": str,
            }
        """
        # Manual forced config (highest priority)
        forced_mode = settings.QUERY_CONCURRENCY_MODE
        forced_max = settings.QUERY_MAX_CONCURRENT

        if forced_mode == "serial":
            return {
                "mode": "serial",
                "max_concurrent": 1,
                "reason": "Manually configured forced serial mode (OPENLAD_QUERY_CONCURRENCY_MODE=serial)",
            }
        if forced_mode == "parallel" and forced_max > 1:
            return {
                "mode": "semaphore",
                "max_concurrent": forced_max,
                "reason": f"Manually configured parallel mode (OPENLAD_QUERY_MAX_CONCURRENT={forced_max})",
            }

        # External mode default: serial execution, safer
        return {
            "mode": "serial",
            "max_concurrent": 1,
            "reason": "External mode default serial — adjust via OPENLAD_QUERY_CONCURRENCY_MODE/OPENLAD_QUERY_MAX_CONCURRENT",
        }

    def get_rate_limits(self) -> Dict:
        """Get current rate limit quotas to apply"""
        plan = self.get_plan()
        return {
            "query_per_minute": plan.per_user_query_per_minute,
            "upload_per_minute": plan.per_user_upload_per_minute,
        }

    def to_dict(self) -> Dict:
        """Serialize to dict (for API response)"""
        plan = self.get_plan()
        snap = self._snapshot
        return {
            "capacity": {
                "max_tenants": plan.max_tenants,
                "max_concurrent_queries": plan.max_concurrent_queries,
                "recommended_active_users": plan.recommended_active_users,
                "rate_limits": {
                    "query_per_minute": plan.per_user_query_per_minute,
                    "upload_per_minute": plan.per_user_upload_per_minute,
                },
            },
            "hardware": {
                "memory": {
                    "total_mb": snap.mem_total_mb if snap else 0,
                    "available_mb": snap.mem_available_mb if snap else 0,
                },
                "disk": {
                    "total_gb": snap.disk_total_gb if snap else 0,
                    "free_gb": snap.disk_free_gb if snap else 0,
                },
            },
            "limit_reasons": {
                "memory": plan.memory_limit_reason,
                "disk": plan.disk_limit_reason,
            },
            "deployment_advice": plan.deployment_advice,
        }


# Singleton
_capacity_manager: Optional[ResourceCapacityManager] = None


def get_capacity_manager() -> ResourceCapacityManager:
    global _capacity_manager
    if _capacity_manager is None:
        _capacity_manager = ResourceCapacityManager()
    return _capacity_manager
