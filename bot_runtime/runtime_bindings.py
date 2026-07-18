"""Compatibility dependency binding for extracted runtime mixins.

The legacy entrypoint owns process-wide constants and imported integrations.
Extracted methods keep their original global-name semantics while the monolith
is decomposed by receiving that namespace explicitly during module assembly.
"""

from __future__ import annotations

import functools
import inspect
from types import CodeType, ModuleType
from typing import Any, Callable, Iterable, Mapping


def bind_runtime_namespace(
    module: ModuleType,
    namespace: Mapping[str, Any],
) -> None:
    """Populate unresolved globals used by source-preserving extracted code."""

    target = vars(module)
    for name, value in namespace.items():
        if name.startswith("__") or name in target:
            continue
        target[name] = value


def sync_runtime_namespace(
    module: ModuleType,
    namespace: Mapping[str, Any],
    *,
    names: Iterable[str] | None = None,
) -> None:
    """Refresh only the runtime globals referenced by an extracted callable."""

    target = vars(module)
    source_items = (
        namespace.items()
        if names is None
        else (
            (name, namespace[name])
            for name in names
            if name in namespace
        )
    )
    for name, value in source_items:
        if name.startswith("__"):
            continue
        if target.get(name) is not value:
            target[name] = value


def _code_global_names(code: CodeType) -> frozenset[str]:
    names = set(code.co_names)
    for constant in code.co_consts:
        if isinstance(constant, CodeType):
            names.update(_code_global_names(constant))
    return frozenset(names)


def build_runtime_proxy(
    function: Callable[..., Any],
    *,
    modules: Iterable[ModuleType],
    namespace: Mapping[str, Any],
) -> Callable[..., Any]:
    """Wrap an extracted function so legacy runtime overrides remain visible."""

    runtime_modules = tuple(modules)
    runtime_names = _code_global_names(function.__code__)

    def _sync() -> None:
        for module in runtime_modules:
            sync_runtime_namespace(
                module,
                namespace,
                names=runtime_names,
            )

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_proxy(*args, **kwargs):
            _sync()
            return await function(*args, **kwargs)

        proxy = async_proxy
    else:
        @functools.wraps(function)
        def sync_proxy(*args, **kwargs):
            _sync()
            return function(*args, **kwargs)

        proxy = sync_proxy
    proxy.__runtime_original__ = function
    return proxy


def proxy_module_classes(
    module: ModuleType,
    *,
    namespace: Mapping[str, Any],
) -> None:
    """Make extracted class methods observe current composition-root globals."""

    for candidate in tuple(vars(module).values()):
        if not inspect.isclass(candidate) or candidate.__module__ != module.__name__:
            continue
        for name, descriptor in tuple(vars(candidate).items()):
            descriptor_type = None
            function = descriptor
            if isinstance(descriptor, property):
                def _proxy_accessor(accessor):
                    if accessor is None:
                        return None
                    if getattr(accessor, "__runtime_original__", None) is not None:
                        return accessor
                    return build_runtime_proxy(
                        accessor,
                        modules=(module,),
                        namespace=namespace,
                    )

                setattr(
                    candidate,
                    name,
                    property(
                        _proxy_accessor(descriptor.fget),
                        _proxy_accessor(descriptor.fset),
                        _proxy_accessor(descriptor.fdel),
                        descriptor.__doc__,
                    ),
                )
                continue
            if isinstance(descriptor, staticmethod):
                descriptor_type = staticmethod
                function = descriptor.__func__
            elif isinstance(descriptor, classmethod):
                descriptor_type = classmethod
                function = descriptor.__func__
            if not inspect.isfunction(function):
                continue
            if getattr(function, "__runtime_original__", None) is not None:
                continue
            proxy = build_runtime_proxy(
                function,
                modules=(module,),
                namespace=namespace,
            )
            if descriptor_type is not None:
                proxy = descriptor_type(proxy)
            setattr(candidate, name, proxy)
