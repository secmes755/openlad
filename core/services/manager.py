"""
External Model Service Manager (External mode)
- URL health check
- Event log recording
- Does not start/manage any processes — user provides LLM / Embedding API themselves
"""
import logging
import time
import json
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

from ..config import settings
from ..db.system_db import get_system_db

logger = logging.getLogger(__name__)


@dataclass
class ServiceStatus:
    name: str
    url: str
    status: str = "unknown"
    http_status: Optional[int] = None
    last_error: Optional[str] = None


class ServiceManager:
    """External Model Service Manager (External mode)

    OpenLAD does not start any model processes. Users deploy llama-server / vLLM / Ollama
    or other OpenAI-compatible API backends themselves, configured via environment variables.
    """

    def __init__(self):
        self._ensure_events_table()

    def _check_http(self, url: str, timeout: int = 3) -> tuple:
        """HTTP health check, returns (status, http_code_or_error)"""
        try:
            import requests
            resp = requests.get(f"{url}/models", timeout=timeout)
            if resp.status_code in (200, 401, 404):
                return "ok", resp.status_code
            return "degraded", resp.status_code
        except requests.ConnectionError:
            return "unreachable", None
        except Exception as e:
            return "error", str(e)

    # ------------------------------------------------------------------
    # Event log (retained for tracking API connection state changes)
    # ------------------------------------------------------------------

    def _ensure_events_table(self):
        try:
            db = get_system_db()
            with db.get_connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS service_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        service TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        pid INTEGER,
                        old_pid INTEGER,
                        new_pid INTEGER,
                        message TEXT,
                        details TEXT
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_service_events_time
                    ON service_events(timestamp)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_service_events_service
                    ON service_events(service)
                """)
                conn.commit()
        except Exception as e:
            logger.warning(f"Failed to initialize service_events table: {e}")

    def log_event(self, service: str, event_type: str, message: str,
                  pid: int = None, old_pid: int = None, new_pid: int = None,
                  details: dict = None):
        try:
            db = get_system_db()
            with db.get_connection() as conn:
                conn.execute("""
                    INSERT INTO service_events
                    (timestamp, service, event_type, pid, old_pid, new_pid, message, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    time.time(), service, event_type, pid, old_pid, new_pid,
                    message, json.dumps(details, ensure_ascii=False) if details else None
                ))
                conn.commit()
        except Exception as e:
            logger.error(f"Failed to record service event: {e}")

    def get_logs(self, service: str = None, limit: int = 100,
                 event_type: str = None, since_hours: int = 24) -> List[dict]:
        try:
            db = get_system_db()
            cutoff = time.time() - since_hours * 3600
            query = "SELECT * FROM service_events WHERE timestamp >= ?"
            params = [cutoff]
            if service:
                query += " AND service = ?"
                params.append(service)
            if event_type:
                query += " AND event_type = ?"
                params.append(event_type)
            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            with db.get_connection() as conn:
                rows = conn.execute(query, params).fetchall()
                return [{
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "datetime": datetime.fromtimestamp(r["timestamp"]).isoformat(),
                    "service": r["service"],
                    "event_type": r["event_type"],
                    "pid": r["pid"],
                    "old_pid": r["old_pid"],
                    "new_pid": r["new_pid"],
                    "message": r["message"],
                    "details": json.loads(r["details"]) if r["details"] else None,
                } for r in rows]
        except Exception as e:
            logger.error(f"Failed to query service logs: {e}")
            return []

    # ------------------------------------------------------------------
    # Status check (pure URL probing, no process inspection)
    # ------------------------------------------------------------------

    def get_status(self) -> Dict[str, ServiceStatus]:
        """Perform HTTP health check on all configured service endpoints"""
        result = {}
        services = [
            ("llm", "LLM", settings.LLM_BASE_URL),
            ("embedding", "Embedding", settings.EMBEDDING_API_BASE),
        ]
        for key, name, url in services:
            http_status, code = self._check_http(url)
            svc = ServiceStatus(
                name=name,
                url=url,
                status=http_status,
                http_status=code,
            )
            if http_status != "ok":
                svc.last_error = f"HTTP {code}" if code else "Connection failed"
            result[key] = svc
        return result


_service_manager = None
import threading
_service_manager_lock = threading.Lock()


def get_service_manager() -> ServiceManager:
    global _service_manager
    if _service_manager is None:
        with _service_manager_lock:
            if _service_manager is None:
                _service_manager = ServiceManager()
    return _service_manager
