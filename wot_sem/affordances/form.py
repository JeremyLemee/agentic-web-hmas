from urllib.parse import urlparse
from wot_sem.affordances.json_schema import JSONSchema


class Form:
    _target: str
    _content_type: str
    _operation_types: set
    _subprotocol: str
    _method_name: str

    # Default mapping for standard WoT operation types
    DEFAULT_METHODS = {
        "readproperty": "GET",
        "writeproperty": "PUT",
        "observeproperty": "GET",
        "unobserveproperty": "GET",
        "invokeaction": "POST",
        "subscribeevent": "GET",
        "unsubscribeevent": "GET",
        "readallproperties": "GET",
        "writeallproperties": "PUT",
        "readmultipleproperties": "GET",
        "writemultipleproperties": "PUT",
    }

    def __init__(
        self, href, method_name, media_type, operation_types, subprotocol, json_schema=None
    ):
        self._target = href
        self._method_name = method_name.upper() if method_name else None
        self._content_type = media_type
        self._operation_types = {op.lower() for op in operation_types} if operation_types else set()
        self._subprotocol = subprotocol
        self._json_schema = json_schema

    @property
    def method_name(self):
        return self._method_name

    @property
    def target(self):
        return self._target

    @property
    def content_type(self):
        return self._content_type

    @property
    def operation_types(self):
        return self._operation_types

    @property
    def subprotocol(self):
        return self._subprotocol

    @property
    def json_schema(self):
        return self._json_schema

    @property
    def protocol(self):
        parsed = urlparse(self._target)
        return parsed.scheme.upper() if parsed.scheme else None

    def get_method_name(self, operation_type=None):
        """
        Return the explicitly defined method name if present, otherwise pick the
        default binding for the given operation type.
        """
        if self._method_name:
            return self._method_name
        if not operation_type:
            return None

        operation_key = operation_type.lower()
        return self.DEFAULT_METHODS.get(operation_key)

    def __str__(self):
        schema_part = ", schema=yes" if self._json_schema else ""
        return f"Form(target={self._target}, method={self._method_name}, ops={sorted(self._operation_types)}{schema_part})"


class FormBuilder:
    _target: str
    _content_type: str
    _operation_types: set
    _subprotocol: str
    _method_name: str
    _json_schema: JSONSchema

    def __init__(self, href):
        self._target = href
        self._content_type = "application/json"
        self._operation_types = set()
        self._subprotocol = None
        self._method_name = None
        self._json_schema = None

    def set_method(self, method):
        self._method_name = method
        return self

    def set_content_type(self, type):
        self._content_type = type
        return self

    def add_operation_type(self, type):
        if type:
            self._operation_types.add(str(type).lower())
        return self

    def set_operation_types(self, types):
        self._operation_types = {str(t).lower() for t in types} if types else set()
        return self

    def set_subprotocol(self, subprotocol):
        self._subprotocol = subprotocol
        return self

    def set_json_schema(self, schema: JSONSchema):
        self._json_schema = schema
        return self

    def build(self):
        return Form(
            self._target,
            self._method_name,
            self._content_type,
            self._operation_types,
            self._subprotocol,
            self._json_schema,
        )
