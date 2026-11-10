"""Exception types."""


class LimitPilotError(Exception):
    """A user-facing failure; the CLI prints the message and exits with code 1."""


class KeychainError(LimitPilotError):
    pass


class SwitchError(LimitPilotError):
    pass
