class SynthesisError(Exception):
    """A stable failure code; permanent failures do not consume retries."""

    code = "synthesis_error"
    retryable = False

    def __str__(self):
        return f"{self.code}: {super().__str__()}"


class SourceVersionExpired(SynthesisError):
    code = "source_version_expired"


class GenerationConfigChanged(SynthesisError):
    code = "generation_config_changed"


class ProviderNotConfigured(SynthesisError):
    code = "provider_not_configured"


class DocumentNotFound(SynthesisError):
    code = "document_not_found"


class DocumentPermissionDenied(SynthesisError):
    code = "permission_revoked"


class DocumentNotProcessed(SynthesisError):
    code = "document_not_processed"
    retryable = True


class DocumentNotIndexed(SynthesisError):
    code = "document_not_indexed"
    retryable = True


class ProviderUnavailable(SynthesisError):
    code = "provider_unavailable"
    retryable = True


class InvalidSourceData(SynthesisError):
    code = "invalid_source_data"


class InvalidCitation(SynthesisError):
    code = "invalid_citation"


class BudgetExceeded(SynthesisError):
    code = "budget_exceeded"


class LeaseLost(SynthesisError):
    code = "lease_lost"
