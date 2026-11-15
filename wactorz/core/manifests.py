# SPDX-License-Identifier: Apache-2.0
"""The two unrelated shapes both called "manifest" in this tree.

An *agent* manifest (`agents/{actor_id}/manifest`) says what one agent
publishes and subscribes to; a *node* manifest (`nodes/{node}/manifest`) says
what one machine is. They happen to travel on retained MQTT topics the same
way and nothing else: an agent manifest is read by
:class:`wactorz.agents.main.manifests.ManifestRegistry`, a node manifest by
:mod:`wactorz.agents.main.nodes` and ``commands/info.py``. Keeping them as two
named shapes, rather than one dict both reach into, is the point of this
module.
"""

from typing import Any, TypedDict


class AgentManifest(TypedDict, total=False):
    """What an agent announces about itself, retained on its own manifest topic.

    Every producer writes the first group; the rest is written by exactly one
    producer and read defensively (``.get(key, default) or default``)
    wherever a manifest might lack it -- a remote or older agent can publish
    fewer fields than a current one, and ``manifests.py``'s ``_as_sent``
    round-trips every manifest through JSON before comparing it, so nothing
    here may assume a key survives that a producer did not actually send.

    Written by every agent (``Actor.publish_manifest``):
        name, actor_id, description, publishes, subscribes, capabilities,
        input_schema, output_schema, timestamp
    Added only when artifacts are declared (``Actor._send_manifest``):
        models
    Added only by ``DynamicAgent`` (``agents.dynamic.streams``), which alone
    declares a full topic contract:
        node, subscribes, triggers_when, produces_schema, consumes_schema,
        observed_samples
    Added only for a manifest injected from the catalogue, never over the
    wire (``agents.catalog_agent``):
        stability, experimental, warning, spawnable, catalog
    Added only when a remote manifest is admitted for a node main did not
    place it on itself (``ManifestRegistry._trusted``):
        node
    Added only for an agent a node hosts (``node.hosted.NodeHosted``), so a
    directory built from manifests alone knows a task reaches it on the
    node's own topic:
        node, tasks_by_node
    """

    name: str
    actor_id: str
    description: str
    publishes: list[str]
    subscribes: list[str]
    capabilities: list[str]
    #: Field name -> a description of it, by convention a type and a
    #: sentence (see `Actor.publish_manifest`'s docstring) -- nothing enforces
    #: that the value is a `str`, so this stays as loose as the real contract.
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    timestamp: float
    models: dict[str, str]
    node: str | None
    triggers_when: dict[str, Any]
    produces_schema: dict[str, Any]
    consumes_schema: dict[str, Any]
    observed_samples: dict[str, Any]
    stability: str
    experimental: bool
    warning: str
    spawnable: bool
    catalog: str
    tasks_by_node: bool


class NodeManifest(TypedDict, total=False):
    """What a node says about the machine it runs on, retained on its own manifest topic.

    Sent once at start and after an install (`node.runner._publish_manifest`);
    the heartbeat carries what changes moment to moment instead, see
    :class:`wactorz.core.heartbeats.NodeHeartbeat`. ``version``/``runtime``
    are absent from a node too old to send them -- read as the single-file
    runtime at an unknown version (`agents.main.nodes`).
    """

    node: str
    version: str
    runtime: str
    manifest_v: int
    arch: str
    os: str | None
    os_release: str | None
    python: str
    model: str | None
    container: bool
    cpu_count: int | None
    ram_total_mb: int | None
    swap_total_mb: int | None
    disk: dict[str, Any]
    gpu: list[dict[str, Any]] | None
    devices: list[str] | None
    packages: dict[str, str]
    #: The library agents this node can build, by import path.
    agents: list[str]
    code_bundles: bool
    actor_ids: bool
    tasks_by_node: bool
    resets: bool
    timestamp: float


__all__ = ["AgentManifest", "NodeManifest"]
