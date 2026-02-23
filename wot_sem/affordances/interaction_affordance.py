import abc


class InteractionAffordance:
    _name: str
    _forms: list

    def __init__(self, name, forms=None, title=None, description=None, json_schema=None):
        self._name = name
        self._forms = forms or []
        self._title = title if title is not None else name
        self._description = description
        self._json_schema = json_schema

    @property
    def name(self):
        return self._name

    @property
    def forms(self):
        return self._forms

    @property
    def title(self):
        return self._title

    @property
    def description(self):
        return self._description

    @property
    def json_schema(self):
        return self._json_schema


class InteractionAffordanceBuilder(abc.ABC):
    _name: str
    _forms: list

    def __init__(self, name, forms=None, title=None, description=None, json_schema=None):
        self._name = name
        self._forms = forms or []
        self._title = title
        self._description = description
        self._json_schema = json_schema

    @property
    def name(self):
        return self._name

    @property
    def forms(self):
        return self._forms

    @property
    def title(self):
        return self._title

    @property
    def description(self):
        return self._description

    @property
    def json_schema(self):
        return self._json_schema

    def add_form(self, form):
        self._forms.append(form)
        return self

    def set_title(self, title):
        self._title = title
        return self

    def set_description(self, description):
        self._description = description
        return self

    def set_json_schema(self, json_schema):
        self._json_schema = json_schema
        return self

    @abc.abstractmethod
    def build(self):
        pass
