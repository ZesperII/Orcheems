"""
Example entry point — copy this into your application, do not modify orcheems/ directly.
"""
from fastapi.middleware.cors import CORSMiddleware

import app.sites   # triggers @SiteLoginServiceRegister.register for each site
import app.tasks  # triggers @task_registration(...) for each task

from orcheems import Orcheemstrator, LocalStateStorage, RedisStateStorage, setup_logging, BrowserManager

setup_logging(level="DEBUG", force_color=True)

### Prepare

browser = BrowserManager(
    max_concurrent_contexts = 30,
    context_acquire_timeout = 30,
    close_timeout = 15,
)

state_storage = RedisStateStorage(ttl_seconds = 10800)

operator = Orcheemstrator(browser_manager = browser, state_storage = state_storage)
app = operator.auto_register_and_build()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)