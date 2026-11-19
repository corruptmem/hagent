"""Compare the genai-prices snapshot with the costs that the probes saw.

genai-prices is the price library that Pydantic AI uses for
`RequestUsage.cost`. No API key and no provider call. Run it with:

    uv run --with 'pydantic-ai-slim[openai]' python probe_genai_prices.py

The expected values are the observed `upstream_inference_cost` (OpenRouter)
or the computed cost of the recorded usage, from the 2026-11-14 probes.
"""

from decimal import Decimal

from genai_prices import Usage, calc_price
from genai_prices.data_snapshot import get_snapshot

CASES = (
    # provider, model, usage, observed upstream cost (USD) or None
    (
        "openai",
        "gpt-4o-mini",
        Usage(input_tokens=2571, output_tokens=1),
        Decimal("0.00038625"),
    ),
    (
        "openai",
        "gpt-4o-mini",
        Usage(input_tokens=2571, cache_read_tokens=2432, output_tokens=1),
        Decimal("0.00020385"),
    ),
    (
        "openai",
        "gpt-6-luna",
        Usage(input_tokens=2570, cache_write_tokens=2567, output_tokens=5),
        Decimal("0.000323675"),
    ),
    (
        "openai",
        "gpt-6-luna",
        Usage(input_tokens=2570, cache_read_tokens=2567, output_tokens=5),
        Decimal("0.00002847"),
    ),
    (
        "typesafe",
        "jev-1.14-20261014",
        Usage(input_tokens=3601, output_tokens=32),
        Decimal("0.000151242"),
    ),
    (
        "openai-decisions",
        "gpt-6-luna",
        Usage(input_tokens=2666, cache_read_tokens=2563),
        None,
    ),
    ("openai", "text-embedding-3-small", Usage(input_tokens=14), Decimal("2.8e-7")),
)


def main() -> None:
    for provider, model, usage, observed in CASES:
        price = calc_price(usage, model_ref=model, provider_id=provider)
        print(
            f"{provider:17} {model:22} computed={price.total_price} "
            f"observed={observed} "
            f"match={'n/a' if observed is None else observed == price.total_price}"
        )
    snapshot = get_snapshot()
    for prov in snapshot.providers:
        if prov.id in {"openai-decisions", "typesafe"}:
            for m in prov.models:
                print(f"snapshot {prov.id} {m.id}: {m.prices}")


if __name__ == "__main__":
    main()
