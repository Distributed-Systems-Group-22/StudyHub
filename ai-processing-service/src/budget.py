"""Per-attempt resource limits shared by transcription, summary and embedding calls."""
from contextvars import ContextVar
import time

current_budget = ContextVar('processing_budget', default=None)


class BudgetExceeded(ValueError):
    """A document requires more work than the configured per-attempt allowance."""


class TaskBudget:
    def __init__(self, max_calls, max_seconds):
        self.max_calls = max_calls
        self.deadline = time.monotonic() + max_seconds
        self.calls = 0

    def remaining(self):
        """Check the wall-clock deadline at stage and network boundaries."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExceeded('Document processing deadline exceeded')
        return remaining

    def call(self, function, *args):
        """Reserve a model call before starting it; never exceed the call budget."""
        self.remaining()
        if self.calls >= self.max_calls:
            raise BudgetExceeded('Document model-call budget exceeded')
        self.calls += 1
        token = current_budget.set(self)
        try:
            result = function(*args)
            self.remaining()
            return result
        finally:
            current_budget.reset(token)


class BudgetedModels:
    """Decorate existing model adapters without changing provider wire formats."""
    def __init__(self, client, budget):
        self.client, self.budget = client, budget
        self.settings = client.settings

    def summarize(self, text):
        return self.budget.call(self.client.summarize, text)

    def embed(self, text):
        return self.budget.call(self.client.embed, text)

    def transcribe(self, contents, filename):
        return self.budget.call(self.client.transcribe, contents, filename)

    def release(self, task):
        # Cleanup must still run after the task exhausts its computation budget.
        self.client.release(task)
