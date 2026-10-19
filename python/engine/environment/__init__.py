"""Environment data for radar analysis: model fields, reanalysis, soundings, satellite, gauges.

A small framework that every outside data source plugs into, so each one behaves the same way:

* **consent first**: ``estimate()`` says what would be downloaded (source, files, size, where it
  goes, licence); nothing is fetched until the user approves that estimate;
* **polite**: honest User-Agent, retries with backoff, a limit on simultaneous connections,
  hard timeouts, a size cap per transfer;
* **cached**: a size-limited LRU folder with checksums; what was fetched once works offline;
* **offline mode**: never touches the network; cached data keeps working;
* **attributed**: every provider carries its attribution and licence text, and what it returns
  carries a provenance record (observed / model / retrieved / simulated / stylised).

Typical use (a provider is registered once, then)::

    from engine.environment import Environment, Request

    env = Environment.default()
    req = Request(provider="iem_raob", time_start="2022-03-30T00:00:00Z",
                  params={"station": "KJAN"})
    est = env.estimate(req)                  # nothing downloaded yet; show est.to_dict()
    result = env.fetch(req, est.approve())   # only after the user said yes
    result.paths, result.provenance

See ``docs/ENVIRONMENT.md`` for how to write a provider.
"""

from __future__ import annotations

from engine.environment.cache import Cache
from engine.environment.core import Environment, Registry
from engine.environment.errors import (
    ApprovalRequired,
    ChecksumMismatch,
    EnvironmentDataError,
    FetchCancelled,
    FetchFailed,
    InvalidRequest,
    OfflineError,
    RequestTooLarge,
    UnknownProvider,
)
from engine.environment.net import HttpClient
from engine.environment.provider import Provider
from engine.environment.registry import register_provider
from engine.environment.request import (
    Approval,
    Estimate,
    FetchResult,
    FileSpec,
    InventoryItem,
    Request,
)
from engine.environment.settings import EnvSettings
from engine.environment.subsetting import subset_dataset

__all__ = [
    "Approval",
    "ApprovalRequired",
    "Cache",
    "ChecksumMismatch",
    "EnvSettings",
    "Environment",
    "EnvironmentDataError",
    "Estimate",
    "FetchCancelled",
    "FetchFailed",
    "FetchResult",
    "FileSpec",
    "HttpClient",
    "InvalidRequest",
    "InventoryItem",
    "OfflineError",
    "Provider",
    "Registry",
    "Request",
    "RequestTooLarge",
    "UnknownProvider",
    "register_provider",
    "subset_dataset",
]
