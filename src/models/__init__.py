"""Modelos ORM — registro por descubrimiento automático.

Importar cada módulo de este paquete ejecuta el cuerpo de sus clases, lo que las
registra en `Base.metadata` (necesario para que SQLAlchemy conozca las tablas).

Para añadir un modelo nuevo basta con crear `src/models/mi_modelo.py` con una clase
que herede de `Base`. **No** hay que editar este archivo: así varios devs añaden
modelos en paralelo sin tocar un archivo compartido (cero conflictos de merge).
"""

import importlib
import inspect
import pkgutil

from src.db.base import Base

__all__: list[str] = []

for _module_info in sorted(_m.name for _m in pkgutil.iter_modules(__path__)):
    _module = importlib.import_module(f"{__name__}.{_module_info}")
    for _name, _obj in inspect.getmembers(_module, inspect.isclass):
        # Solo modelos declarados en este módulo (no los importados de otro lado).
        if issubclass(_obj, Base) and _obj is not Base and _obj.__module__ == _module.__name__:
            globals()[_name] = _obj
            __all__.append(_name)
