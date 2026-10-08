"""Per-lead cost tracking (moved unchanged from first_graph.py)."""
from dataclasses import dataclass


@dataclass
class LeadCost:
    """Tracks cost metrics for a single lead."""
    lead_id: int
    business_name: str
    haiku_input_tokens: int = 0
    haiku_output_tokens: int = 0
    sonnet_input_tokens: int = 0
    sonnet_output_tokens: int = 0
    brave_calls: int = 0
    haiku_errors: list = None
    sonnet_errors: list = None
    brave_errors: list = None
    brave_429: int = 0                  # recording only: Brave 429 responses seen
    brave_429_recovered: int = 0        # of those, the retry came back without a 429
    
    def __post_init__(self):
        if self.brave_errors is None:
            self.brave_errors = []
        if self.haiku_errors is None:
            self.haiku_errors = []
        if self.sonnet_errors is None:
            self.sonnet_errors = []
    
    @property
    def total_tokens(self) -> int:
        return self.haiku_input_tokens + self.haiku_output_tokens + \
               self.sonnet_input_tokens + self.sonnet_output_tokens
    
    def estimated_cost_usd(self) -> float:
        """Estimate cost at current Claude pricing (as of 2026-08)."""
        # Haiku: $0.80 / 1M input, $4.00 / 1M output
        haiku_cost = (self.haiku_input_tokens * 0.80 + self.haiku_output_tokens * 4.00) / 1_000_000
        # Sonnet: $3.00 / 1M input, $15.00 / 1M output
        sonnet_cost = (self.sonnet_input_tokens * 3.00 + self.sonnet_output_tokens * 15.00) / 1_000_000
        # Brave: ~$0.10 per request (flat)
        brave_cost = self.brave_calls * 0.005
        return haiku_cost + sonnet_cost + brave_cost
