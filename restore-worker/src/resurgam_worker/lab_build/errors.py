class StepFailed(Exception):
    """Ends the job: the current step is marked failed with this message."""

    def __init__(self, code: str, message: str, output: str | None = None):
        self.code = code
        self.message = message
        self.output = output
        super().__init__(message)
