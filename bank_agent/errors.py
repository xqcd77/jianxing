class BankError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class ClarificationNeeded(BankError):
    def __init__(self, message):
        super().__init__("CLARIFICATION_REQUIRED", message, 422)
