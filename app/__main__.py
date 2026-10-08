"""python -m app  ->  serve on PORT (default 8000)."""
import logging
import os

import uvicorn

from .server import create_app

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per REST call is just noise

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                log_level="warning")
