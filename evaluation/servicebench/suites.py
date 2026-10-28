"""Explicit suite registry; a design-only adapter is never an application run."""
VERSIONS = {
    "base64": "finservicebench-64-v3-task-clarity",
    "specialist20": "bankflow-specialist20-application-v1",
    "combined84": "bankflow84-application-v1",
    "lite48": "bankflow-lite48-v1-20261018",
    "lite58": "bankflow-lite58-v1-20261024",
    'lite66': 'bankflow-lite66-v1-20261027',
}


def scenarios(suite="base64"):
    from .catalog import scenarios as base
    if suite == "base64":
        return base()
    if suite == "lite48":
        from .lite48 import scenarios as lite
        return lite()
    if suite == "lite58":
        from .lite58 import scenarios as lite
        return lite()
    if suite == 'lite66':
        from .lite66 import scenarios as lite
        return lite()
    from .specialist import scenarios as specialist
    if suite == "specialist20":
        return specialist()
    if suite == "combined84":
        return base() + specialist()
    raise ValueError("Unknown application suite: " + suite)
