from domain.costs import ModelUsage


def normalize_openai_usage(usage: object | None) -> ModelUsage:
    """Translate common OpenAI usage fields into the provider-neutral contract."""
    if usage is None:
        return ModelUsage()
    input_details = getattr(usage, "input_tokens_details", None)
    output_details = getattr(usage, "output_tokens_details", None)
    return ModelUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        reasoning_tokens=int(getattr(output_details, "reasoning_tokens", 0) or 0),
    )
