"""Python (PyDABs) resources for the agent memory pipeline bundle.

Defines the resource types that have typed databricks-bundles builders:
  * Unity Catalog schema (holds the dreamer_logs volume)
  * UC volume `dreamer_logs` (dreamer distillation reports)
  * Dreamer batch jobs (added in a later phase)

Resource types without a Python builder in databricks-bundles 1.4.0
(database_instance, experiment, app) are declared in resources/*.yml.

``${...}`` strings are resolved by the Databricks CLI after this loader runs, so
referencing variables and other resources by interpolation is safe here.
"""

from databricks.bundles.core import Bundle, Resources
from databricks.bundles.schemas import Schema
from databricks.bundles.volumes import Volume

# Resource keys (stable identifiers used for cross-references and `bundle` CLI output).
SCHEMA_KEY = "memory_demo_schema"
VOLUME_KEY = "dreamer_logs"


def load_resources(bundle: Bundle) -> Resources:
    resources = Resources()

    resources.add_schema(
        SCHEMA_KEY,
        Schema(
            catalog_name="${var.catalog}",
            name="${var.schema_name}",
            comment="Agent memory pipeline demo: holds the dreamer_logs volume.",
        ),
    )

    resources.add_volume(
        VOLUME_KEY,
        Volume(
            catalog_name="${var.catalog}",
            # Reference the schema resource so we pick up dev-mode name prefixing.
            schema_name=f"${{resources.schemas.{SCHEMA_KEY}.name}}",
            name="dreamer_logs",
            comment="Dreamer distillation markdown reports/logs.",
        ),
    )

    return resources
