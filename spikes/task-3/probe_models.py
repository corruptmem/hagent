"""Probe the models endpoints of both providers for price fields. Free."""

from _http import call, dump

WANTED = (
    "openai/gpt-5-nano",
    "openai/gpt-4o-mini",
    "openai/gpt-6-luna",
    "openai/gpt-6-luna:batch",
)
ALPHA_PATHS = (
    "https://openrouter.ai/api/alpha/models",
    "https://openrouter.ai/api/alpha/decisions/models",
)


def openrouter_models() -> None:
    models, _ = call("openrouter", "/models")
    data = models.get("data", [])
    print(f"openrouter /models count={len(data)}")
    for m in data:
        if m["id"] in WANTED:
            dump(m["id"], {"pricing": m.get("pricing"), "links": m.get("links")})
    pricing_keys: set[str] = set()
    for m in data:
        pricing_keys.update((m.get("pricing") or {}).keys())
    dump("openrouter union of pricing keys", sorted(pricing_keys))
    dynamic = [m["id"] for m in data if (m.get("pricing") or {}).get("prompt") == "-1"]
    dump("openrouter models with dynamic price -1", dynamic)
    tiered = [m["id"] for m in data if "overrides" in (m.get("pricing") or {})]
    print(f"openrouter models with tiered 'overrides': {len(tiered)}")
    jev = [m["id"] for m in data if "jev" in m["id"]]
    dump("openrouter /models ids with 'jev'", jev)


def openrouter_embedding_models() -> None:
    emb, _ = call("openrouter", "/embeddings/models")
    data = emb.get("data", [])
    print(f"openrouter /embeddings/models count={len(data)}")
    for m in data:
        if m.get("id") == "openai/text-embedding-3-small":
            dump(m["id"], m.get("pricing"))


def openrouter_alpha() -> None:
    for path in ALPHA_PATHS:
        resp, _ = call("openrouter", path)
        print(path, resp.get("http_error", "200"))


def openai_models() -> None:
    oa, _ = call("openai", "/models/gpt-6-luna")
    dump("openai /models/gpt-6-luna (no price fields)", oa)


def main() -> None:
    openrouter_models()
    openrouter_embedding_models()
    openrouter_alpha()
    openai_models()


if __name__ == "__main__":
    main()
