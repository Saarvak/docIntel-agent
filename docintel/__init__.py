"""DocIntel document processing: MinerU parsing, field extraction and business-rule validation."""

try:
    from .pipeline import analyze_document
    __all__ = ["analyze_document"]
except ImportError:
    # Hosted Agentverse flattens this file to the agent root; do not import siblings here.
    __all__: list[str] = []
