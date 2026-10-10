"""Application error types. Messages are meant to be shown to the user."""


class StudioError(Exception):
    """Base class for expected, user-reportable errors."""


class DependencyMissingError(StudioError):
    """A required external tool (e.g. FFmpeg) is not available."""


class MediaError(StudioError):
    """A media file is missing, unreadable, or not a supported video."""


class ProjectError(StudioError):
    """A project file could not be read or written."""


class JobCancelled(StudioError):
    """Raised inside a job handler when the user cancelled the job."""


class InvalidStateTransition(StudioError):
    """A job was asked to move to a state that is not allowed from its current state."""


class SettingsError(StudioError):
    """A setting value failed validation."""
