"""Plain-data YAML reads and writes, with ruamel.yaml in pure Python so no compiled package is needed on a new Python.
The rules are PyYAML's (what the console used before): YAML 1.1, `yes`/`on` are booleans, `y`/`n` and `1e3` are strings."""
import io
import re

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.resolver import VersionedResolver, implicit_resolvers
from ruamel.yaml.util import RegExp

__all__ = ["load", "dump", "YAMLError"]

_PYYAML = {
    "tag:yaml.org,2002:bool": RegExp(r"^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$"),
    "tag:yaml.org,2002:float": RegExp(r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
        |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
        |[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*
        |[-+]?\.(?:inf|Inf|INF)
        |\.(?:nan|NaN|NAN))$""", re.X),
}


class _PyYamlResolver(VersionedResolver):
    """ruamel's YAML 1.1 rules with PyYAML's booleans and floats, without writing a `%YAML 1.1` line."""

    @property
    def processing_version(self):
        return (1, 1)

    @property
    def versioned_resolver(self):
        table = self._version_implicit_resolver
        if (1, 1) not in table:
            for versions, tag, regexp, first in implicit_resolvers:
                if (1, 1) in versions:
                    self.add_version_implicit_resolver((1, 1), tag, _PYYAML.get(tag, regexp), first)
        return table[(1, 1)]


def _yaml():
    yaml = YAML(typ="safe", pure=True)  # a new one per call: a YAML instance isn't thread-safe
    yaml.Resolver = _PyYamlResolver
    return yaml


def load(source):
    """A str, bytes or open file → plain Python data (None for an empty document)."""
    return _yaml().load(source)


def dump(data):
    """Plain data → block-style YAML text, keys in their own order."""
    yaml = _yaml()
    yaml.default_flow_style = False
    yaml.representer.sort_base_mapping_type_on_output = False
    out = io.StringIO()
    yaml.dump(data, out)
    return out.getvalue()
