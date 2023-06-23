"""
Functions to use with the Router.
"""
from importlib import import_module
from typing import TYPE_CHECKING, Any, List, Optional, Union

from esmerald.exceptions import ImproperlyConfigured

if TYPE_CHECKING:
    from esmerald.routing.gateways import Gateway, WebSocketGateway
    from esmerald.routing.router import Include, Router

DEFAULT_PATTERN = "route_patterns"


def include(
    arg: Any, pattern: Optional[str] = DEFAULT_PATTERN
) -> List[Union["Gateway", "WebSocketGateway", "Router", "Include"]]:
    """Simple retrieve functionality to make it easier to include
    routes in the urls. Example, nested routes.

    Example:

        # myapp.routes.py

        from esmerald import Router
        from .views import MyView

        router_patterns = [
            Router(path='/my-iew', routes=[MyView])
        ]

        # routers.py
        from esmerald import Router
        from esmerald.conf.urls import include

        router_patterns = [
            Router(path='/', routes=[include('myapp.routes')])
        ]

    If a `routes.py ` doesn't contain a default `router_patterns` that can be
    also specified dynamically.

    Example:

        # myapp.routes.py

        from esmerald import Router
        from .views import MyView

        myapp_urls = [
            Router(path='/my-iew', route_handlers=[MyView])
        ]

        # routers.py
        from esmerald import Router
        from esmerald.conf.urls import include

        router_patterns = [
            Router(path='/', routes=[include('myapp.routes', pattern='myapp_urls')])
        ]


    Args:
        namespace (Any): Example: 'mymodule.urls'
        pattern (Optional[str], optional): The name of the list to be read from the
        module. Defaults to `router_patterns`.
    """

    if not isinstance(arg, str):
        raise ImproperlyConfigured("The value should be a string with the format <module>.<file>")

    router_conf_module = import_module(arg)
    pattern = pattern or DEFAULT_PATTERN
    patterns = getattr(router_conf_module, pattern, None)

    if not patterns:
        raise ImproperlyConfigured(
            f"There is no pattern {pattern} found in {arg}. "
            "Are you sure you configured it correctly?"
        )

    if not isinstance(patterns, list):
        raise ImproperlyConfigured(f"{patterns} should be a list and not {type(patterns)}.")

    return patterns
