__all__ = ["AgentState", "IntakeOutput", "SimContext", "build_graph"]


def __getattr__(name):
    """Load the optional legacy pipeline only when that public API is requested."""
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    module = import_module("src.agent.legacy.graph" if name == "build_graph" else "src.agent.state")
    value = getattr(module, name)
    globals()[name] = value
    return value
