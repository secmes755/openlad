"""
OpenLAD Entry Point
Launch FastAPI service
"""
import logging
import os
import sys

# Ensure OpenLAD is on the path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    import uvicorn
    from core.config import settings

    logger.info(f"=" * 60)
    logger.info(f"OpenLAD starting...")
    logger.info(f"Data directory: {settings.DATA_DIR}")
    logger.info(f"API address: http://{settings.API_HOST}:{settings.API_PORT}")
    logger.info(f"=" * 60)

    uvicorn.run(
        "core.api.main:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=False,
        log_level="info"
    )


if __name__ == "__main__":
    main()
