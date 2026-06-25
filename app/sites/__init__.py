import importlib
import pkgutil
from pathlib import Path

for _, module_name, _ in pkgutil.iter_modules([str(Path(__file__).parent)]):
    importlib.import_module(f"app.sites.{module_name}")