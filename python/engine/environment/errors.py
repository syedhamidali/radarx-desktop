# ruff: noqa: N818
"""Exceptions of the environment-data framework (all derive from :class:`EnvironmentDataError`).

Every message is written for the person at the keyboard: what happened and what to do.
"""

from __future__ import annotations


class EnvironmentDataError(Exception):
    """Base class: something went wrong while planning or fetching environment data."""


class OfflineError(EnvironmentDataError):
    """A network access was needed but offline mode is on (or the network is unreachable)."""


class ApprovalRequired(EnvironmentDataError):
    """A fetch that would download data was started without a matching, explicit approval."""


class RequestTooLarge(EnvironmentDataError):
    """The request is larger than the cache limit or than what the user approved."""


class ChecksumMismatch(EnvironmentDataError):
    """A downloaded file does not match its published checksum (so it was not kept)."""


class FetchCancelled(EnvironmentDataError):
    """The user cancelled the fetch; nothing incomplete is kept."""


class FetchFailed(EnvironmentDataError):
    """A download failed for good (after the polite retries)."""


class InvalidRequest(EnvironmentDataError, ValueError):
    """The request is malformed (bad area, time, variable list ...)."""


class UnknownProvider(EnvironmentDataError, KeyError):
    """No provider with that id is registered."""

    def __str__(self) -> str:  # KeyError would repr() the message
        return str(self.args[0]) if self.args else "unknown provider"
