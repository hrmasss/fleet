"""What "shipped" means for a given project.

The gate's four checks are generic — merged, built, running, recorded — but the
coordinates they run against are not. This is the one place those coordinates live, so
the gate never learns a repo name and fleet stays usable for a second project by writing
a second config rather than editing logic.

⚠ There is no built-in project. Every field comes from the config file or the
environment, and a missing one is an error naming the key. An earlier version shipped one
installation's coordinates as defaults, which meant a fresh checkout would happily run its
production checks against somebody else's containers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from fleet import config


@dataclass(frozen=True, slots=True)
class Project:
    """Coordinates for checking whether a merge reached production."""

    repo: str
    """GitHub `owner/name`, passed to `gh --repo`."""

    workspace: Path
    """The repo holding session files. Not the code repo."""

    code_repo: Path
    """A local clone of the code repo, used only to resolve short shas and test ancestry."""

    prod_host: str
    """SSH alias for the production box."""

    prod_containers: tuple[str, ...]
    """⚠ Exact container names, never substrings. A `myapp-staging-web-1` beside a
    `myapp-web-1` on the same daemon means a substring match reads staging as production
    and reports a deploy that never happened."""

    build_workflow: str
    """`gh run list --workflow` value for the job that builds and pushes images."""

    deploy_workflow: str

    tag_prefix: str = "sha-"
    """Image tags are this plus the short commit, e.g. `ghcr.io/owner/name:sha-789c359`."""

    sessions_subdir: str = "todo/sessions"

    extra: dict[str, str] = field(default_factory=dict)

    @property
    def sessions(self) -> Path:
        return self.workspace / self.sessions_subdir

    @classmethod
    def load(cls) -> Project:
        """Build from the config file, with the environment winning where it speaks."""
        return cls(
            repo=config.require("project", "repo", env="FLEET_REPO"),
            workspace=Path(
                config.require("project", "workspace", env="FLEET_WORKSPACE")
            ).expanduser(),
            code_repo=Path(
                config.require("project", "code_repo", env="FLEET_CODE_REPO")
            ).expanduser(),
            prod_host=config.require("project", "prod_host", env="FLEET_PROD_HOST"),
            prod_containers=config.get_list(
                "project", "prod_containers", env="FLEET_PROD_CONTAINERS"
            ),
            build_workflow=config.get(
                "project", "build_workflow", "images", env="FLEET_BUILD_WORKFLOW"
            ),
            deploy_workflow=config.get(
                "project", "deploy_workflow", "deploy", env="FLEET_DEPLOY_WORKFLOW"
            ),
            tag_prefix=config.get("project", "tag_prefix", "sha-"),
            sessions_subdir=config.get("project", "sessions_subdir", "todo/sessions"),
        )
