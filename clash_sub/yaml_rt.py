import copy
from collections.abc import Mapping
from io import StringIO

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq


class RoundTripYamlError(ValueError):
    pass


def _yaml():
    parser = YAML(typ="rt")
    parser.allow_duplicate_keys = False
    parser.preserve_quotes = True
    parser.width = 4096
    return parser


def load_round_trip(payload):
    try:
        text = payload.decode("utf-8") if isinstance(payload, bytes) else payload
        document = _yaml().load(text)
    except Exception:
        raise RoundTripYamlError("yaml round trip failed") from None
    if not isinstance(document, CommentedMap):
        raise RoundTripYamlError("yaml root must be a mapping")
    return document


def dump_round_trip(document):
    stream = StringIO()
    try:
        _yaml().dump(document, stream)
    except Exception:
        raise RoundTripYamlError("yaml round trip failed") from None
    text = stream.getvalue()
    return text if text.endswith("\n") else text + "\n"


def clone_round_trip(value):
    return copy.deepcopy(value)


def copy_key_comments(source, source_key, target, target_key):
    if source.ca.comment is not None:
        target.ca.comment = copy.deepcopy(source.ca.comment)
    if source_key in source.ca.items:
        target.ca.items[target_key] = copy.deepcopy(source.ca.items[source_key])


def plain_data(value):
    if isinstance(value, Mapping):
        return {key: plain_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, CommentedSeq)):
        return [plain_data(item) for item in value]
    return copy.deepcopy(value)
