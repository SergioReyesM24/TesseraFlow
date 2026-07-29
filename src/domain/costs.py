"""Provider-neutral model usage, pricing, and per-turn cost accounting."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

ZERO = Decimal("0")
TOKENS_PER_MILLION = Decimal("1000000")


@dataclass(frozen=True, slots=True)
class ModelUsage:
    """Normalized token counters reported by any model adapter."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cached_input_audio_tokens: int = 0
    reasoning_tokens: int = 0
    input_audio_tokens: int = 0
    output_audio_tokens: int = 0

    def __post_init__(self) -> None:
        """Reject impossible counters before they reach billing or persistence."""
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.cached_input_audio_tokens,
            self.reasoning_tokens,
            self.input_audio_tokens,
            self.output_audio_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("Model usage counters cannot be negative")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("Cached input tokens cannot exceed input tokens")
        if self.input_audio_tokens > self.input_tokens:
            raise ValueError("Input audio tokens cannot exceed input tokens")
        if self.cached_input_audio_tokens > self.cached_input_tokens:
            raise ValueError("Cached audio tokens cannot exceed cached input tokens")
        if self.cached_input_audio_tokens > self.input_audio_tokens:
            raise ValueError("Cached audio tokens cannot exceed input audio tokens")
        categorized_input = (
            self.cached_input_tokens
            + self.input_audio_tokens
            - self.cached_input_audio_tokens
        )
        if categorized_input > self.input_tokens:
            raise ValueError("Cached and audio input categories exceed input tokens")
        if self.output_audio_tokens > self.output_tokens:
            raise ValueError("Output audio tokens cannot exceed output tokens")
        if self.reasoning_tokens > self.output_tokens:
            raise ValueError("Reasoning tokens cannot exceed output tokens")

    @property
    def total_tokens(self) -> int:
        """Return provider-compatible input plus output tokens."""
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        """Return billed input not served from cache, including retained context."""
        return self.input_tokens - self.cached_input_tokens

    def __add__(self, other: "ModelUsage") -> "ModelUsage":
        """Aggregate model calls without losing billable token categories."""
        return ModelUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cached_input_audio_tokens=(
                self.cached_input_audio_tokens + other.cached_input_audio_tokens
            ),
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            input_audio_tokens=self.input_audio_tokens + other.input_audio_tokens,
            output_audio_tokens=self.output_audio_tokens + other.output_audio_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelRateTier:
    """Alternative rates activated by the input size of one model request."""

    min_input_tokens: int
    input: Decimal
    output: Decimal
    cached_input: Decimal | None = None
    cached_input_audio: Decimal | None = None
    input_audio: Decimal | None = None
    output_audio: Decimal | None = None

    def __post_init__(self) -> None:
        rates = (
            self.input,
            self.output,
            self.cached_input,
            self.cached_input_audio,
            self.input_audio,
            self.output_audio,
        )
        if self.min_input_tokens < 1:
            raise ValueError("Model rate tier threshold must be positive")
        if any(rate is not None and rate < ZERO for rate in rates):
            raise ValueError("Model tier rates cannot be negative")


@dataclass(frozen=True, slots=True)
class ModelRates:
    """Price card per million tokens for one model, independent of its provider."""

    input: Decimal
    output: Decimal
    cached_input: Decimal | None = None
    cached_input_audio: Decimal | None = None
    input_audio: Decimal | None = None
    output_audio: Decimal | None = None
    currency: str = "USD"
    tiers: tuple[ModelRateTier, ...] = ()

    def __post_init__(self) -> None:
        """Keep rate cards internally consistent and safe to calculate."""
        rates = (
            self.input,
            self.output,
            self.cached_input,
            self.cached_input_audio,
            self.input_audio,
            self.output_audio,
        )
        if any(rate is not None and rate < ZERO for rate in rates):
            raise ValueError("Model rates cannot be negative")
        if not self.currency.strip():
            raise ValueError("Model rate currency cannot be empty")
        thresholds = [tier.min_input_tokens for tier in self.tiers]
        if len(thresholds) != len(set(thresholds)):
            raise ValueError("Model rate tier thresholds must be unique")


@dataclass(frozen=True, slots=True)
class ModelCost:
    """Exact calculated monetary amount for one or more model calls."""

    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.amount < ZERO:
            raise ValueError("Model cost cannot be negative")
        if not self.currency.strip():
            raise ValueError("Model cost currency cannot be empty")


@dataclass(frozen=True, slots=True)
class ModelCallMetrics:
    """Usage and optional configured cost for one provider request."""

    model: str
    usage: ModelUsage
    cost: ModelCost | None = None


@dataclass(frozen=True, slots=True)
class TurnMetrics:
    """Every model request made while producing one logical agent turn."""

    calls: tuple[ModelCallMetrics, ...] = ()

    @property
    def usage(self) -> ModelUsage:
        """Aggregate every tool round into one turn-level usage view."""
        total = ModelUsage()
        for call in self.calls:
            total += call.usage
        return total

    @property
    def cost(self) -> ModelCost | None:
        """Aggregate cost when all priced calls use the same currency."""
        priced = [call.cost for call in self.calls if call.cost is not None]
        if not priced:
            return None
        if len(priced) != len(self.calls):
            return None
        currencies = {cost.currency for cost in priced}
        if len(currencies) != 1:
            return None
        return ModelCost(
            amount=sum((cost.amount for cost in priced), start=ZERO),
            currency=priced[0].currency,
        )


class ModelCostCalculator:
    """Calculate costs from a model-keyed, provider-neutral rate catalog."""

    def __init__(self, rates: Mapping[str, ModelRates] | None = None) -> None:
        self._rates = dict(rates or {})

    def metrics(self, model: str, usage: ModelUsage) -> ModelCallMetrics:
        """Build one auditable call metric, leaving cost unknown without a rate."""
        rates = self._rates.get(model)
        return ModelCallMetrics(
            model=model,
            usage=usage,
            cost=self._calculate(usage, rates) if rates is not None else None,
        )

    @staticmethod
    def _calculate(usage: ModelUsage, rates: ModelRates) -> ModelCost:
        tier = max(
            (
                candidate
                for candidate in rates.tiers
                if usage.input_tokens >= candidate.min_input_tokens
            ),
            key=lambda candidate: candidate.min_input_tokens,
            default=None,
        )
        input_rate = tier.input if tier is not None else rates.input
        output_rate = tier.output if tier is not None else rates.output
        cached_input_rate = (
            tier.cached_input
            if tier is not None and tier.cached_input is not None
            else rates.cached_input
        )
        cached_audio_rate = (
            tier.cached_input_audio
            if tier is not None and tier.cached_input_audio is not None
            else rates.cached_input_audio
        )
        input_audio_rate = (
            tier.input_audio
            if tier is not None and tier.input_audio is not None
            else rates.input_audio
        )
        output_audio_rate = (
            tier.output_audio
            if tier is not None and tier.output_audio is not None
            else rates.output_audio
        )
        cached = usage.cached_input_tokens
        cached_audio = usage.cached_input_audio_tokens
        cached_text = cached - cached_audio
        uncached_audio = usage.input_audio_tokens - cached_audio
        uncached_text = usage.input_tokens - cached - uncached_audio
        output_audio = usage.output_audio_tokens
        output_text = usage.output_tokens - output_audio
        amount = (
            Decimal(uncached_text) * input_rate
            + Decimal(cached_text) * ModelCostCalculator._rate(cached_input_rate, input_rate)
            + Decimal(cached_audio)
            * ModelCostCalculator._rate(
                cached_audio_rate,
                ModelCostCalculator._rate(cached_input_rate, input_rate),
            )
            + Decimal(uncached_audio) * ModelCostCalculator._rate(input_audio_rate, input_rate)
            + Decimal(output_text) * output_rate
            + Decimal(output_audio) * ModelCostCalculator._rate(output_audio_rate, output_rate)
        ) / TOKENS_PER_MILLION
        return ModelCost(amount=amount, currency=rates.currency)

    @staticmethod
    def _rate(value: Decimal | None, fallback: Decimal) -> Decimal:
        """Honor explicit zero-price categories instead of treating them as absent."""
        return value if value is not None else fallback
