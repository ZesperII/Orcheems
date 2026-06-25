"""
Example entry point — copy this into your application, do not modify orcheems/ directly.
"""
from fastapi.middleware.cors import CORSMiddleware

import app.sites   # triggers @SiteLoginServiceRegister.register for each site
import app.tasks  # triggers @task_registration(...) for each task

from orcheems import Orcheemstrator, LocalStateStorage, RedisStateStorage, setup_logging

setup_logging(level="DEBUG", force_color=True)

operator = Orcheemstrator(state_storage=LocalStateStorage(".cookies"),)
app = operator.auto_register_and_build()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)