# from .register import SiteLoginServiceRegister

# import importlib                                        
# import pkgutil
# from . import sites as sites_pkg

# for _, module_name, _ in pkgutil.iter_modules(sites_pkg.__path__):
#     importlib.import_module(f"login_service.sites.{module_name}")

# __all__ = ["SiteLoginServiceRegister"]

# from .schema import Credential, LoginResult

from .register import SiteLoginServiceRegister
from .schema import Credential, LoginResult
from .base import BaseLoginService, cookie_incomplete_handler

__all__ = [
    "SiteLoginServiceRegister",
    "Credential",
    "LoginResult",
    "BaseLoginService",
    "cookie_incomplete_handler",
]

# Site implementations live in app/sites/ and are discovered by the
# application at startup — not auto-imported here.
# In main.py: import app.sites  (triggers @SiteLoginServiceRegister.register)