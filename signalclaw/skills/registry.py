from typing import Dict, Type, Any
import importlib

SKILL_REGISTRY: Dict[str, Any] = {}

def register_skill(name: str):
    def decorator(cls):
        SKILL_REGISTRY[name] = cls
        return cls
    return decorator

def get_skill(name: str):
    if name not in SKILL_REGISTRY:
        raise KeyError(f"Skill '{name}' not found. Available: {list(SKILL_REGISTRY.keys())}")
    return SKILL_REGISTRY[name]
