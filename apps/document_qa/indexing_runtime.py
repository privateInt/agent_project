"""Read-only selected embedding/chunker gate. Default policy forbids inference."""
from . import readiness
from .indexing_results import defaults_checked
from .intake import check


class IndexingGate:
    def __init__(self, settings, allow_inference=False, reader_factory=None):
        self.settings = settings
        self.allow_inference = allow_inference is True
        self.reader_factory = reader_factory or (lambda budget: readiness.GatewayReader(
            settings['platforms'], budget_seconds=min(20, budget), timeout=8))

    def __call__(self, remaining):
        # Defer before settings, credentials, Model metadata or Knowledge resources.
        check(self.allow_inference, 'GPU_VERIFICATION_DEFERRED', 409)
        defaults = defaults_checked(self.settings.get('defaults'))
        check(remaining > 0, 'INDEXING_FLOW_BUDGET_EXCEEDED', 504)
        reader = self.reader_factory(remaining)
        pinned = defaults['embedding']
        identifier = readiness.identifier(pinned['deployment_id'])
        dep = reader.get('model', '/api/deployments/' + identifier)
        readiness.live_deployment(dep, identifier)
        profile = reader.get('model', '/api/deployments/' + identifier + '/embedding-profile')
        actual = readiness.embedding_profile(dep, profile)
        check(actual == pinned, 'INDEXING_PROFILE_CHANGED', 409)
        catalog = reader.get('knowledge', '/api/chunkers')
        actual_chunker = readiness.chunker(catalog, defaults['chunker'], actual)
        check(actual_chunker == defaults['chunker'], 'INDEXING_CHUNKER_CHANGED', 409)
        return defaults
