"""Public exports loaded only when requested by a caller."""

__all__ = ["ServiceConfig", "ConfigError", "ClashSubService", "ServiceError"]
_EXPORTS = {
    "ServiceConfig": ("clash_sub.domain", "ServiceConfig"),
    "ConfigError": ("clash_sub.config", "ConfigError"),
    "ClashSubService": ("clash_sub.service", "ClashSubService"),
    "ServiceError": ("clash_sub.operation", "ServiceError"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    from importlib import import_module
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value
