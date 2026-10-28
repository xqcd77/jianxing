"""Generate browser TypeScript contracts from the actual FastAPI OpenAPI schema."""
from pathlib import Path
from tempfile import TemporaryDirectory

from bank_agent.app import create_app
from bank_agent.config import Settings


def type_of(s):
    if "$ref" in s:
        return s["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in s:
        return " | ".join(type_of(x) for x in s["anyOf"])
    if "enum" in s:
        import json
        return " | ".join(json.dumps(v, ensure_ascii=False) for v in s["enum"])
    if "const" in s:
        import json
        return json.dumps(s["const"], ensure_ascii=False)
    kind = s.get("type")
    if kind == "array":
        return "Array<" + type_of(s.get("items", {})) + ">"
    if kind == "object":
        props = s.get("properties")
        if props is not None:
            required = set(s.get("required", []))
            return "{ " + "; ".join(k + ("" if k in required else "?") + ": " + type_of(v) for k, v in props.items()) + " }"
        return "Record<string, " + type_of(s.get("additionalProperties", {}) if isinstance(s.get("additionalProperties"), dict) else {}) + ">"
    return {"string": "string", "integer": "number", "number": "number", "boolean": "boolean", "null": "null"}.get(kind, "unknown")


def main():
    with TemporaryDirectory() as folder:
        app = create_app(Settings(data_dir=Path(folder)))
        try:
            schemas = app.openapi()["components"]["schemas"]
        finally:
            app.state.graph.close()
            app.state.db.engine.dispose()
    output = "// Generated from FastAPI OpenAPI. Run scripts/generate_api_types.py; do not edit.\n"
    for name, schema in schemas.items():
        output += "export type " + name + " = " + type_of(schema) + ";\n"
    Path("bank_agent/static/api-contracts.d.ts").write_text(output, encoding="utf-8")


if __name__ == "__main__":
    main()
