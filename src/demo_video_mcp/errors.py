"""Domain errors safe to return through an MCP tool result."""


class VideoMCPError(Exception):
    """Expected user-correctable error."""


class ValidationError(VideoMCPError):
    """Input or state validation failed."""


class NotFoundError(VideoMCPError):
    """Requested local object was not found."""


class ConflictError(VideoMCPError):
    """Requested transition conflicts with current state."""
