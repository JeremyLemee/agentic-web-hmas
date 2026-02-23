from wot_sem.affordances.interaction_affordance import (
    InteractionAffordance,
    InteractionAffordanceBuilder,
)


class PropertyAffordance(InteractionAffordance):
    def __init__(
        self,
        name,
        forms=None,
        title=None,
        description=None,
        read_only=False,
        write_only=False,
        observable=False,
        json_schema=None,
    ):
        super().__init__(name, forms, title, description, json_schema)
        self._read_only = bool(read_only)
        self._write_only = bool(write_only)
        self._observable = bool(observable)

    @property
    def read_only(self):
        return self._read_only

    @property
    def write_only(self):
        return self._write_only

    @property
    def observable(self):
        return self._observable

    def __str__(self):
        flags = []
        if self._read_only:
            flags.append("readOnly")
        if self._write_only:
            flags.append("writeOnly")
        if self._observable:
            flags.append("observable")
        flag_part = f" ({', '.join(flags)})" if flags else ""
        return f"Property '{self._name}'{flag_part}"


class PropertyAffordanceBuilder(InteractionAffordanceBuilder):
    def __init__(self, name, forms=None, title=None, description=None):
        super().__init__(name, forms, title, description)
        self._read_only = False
        self._write_only = False
        self._observable = False

    def set_read_only(self, value=True):
        self._read_only = bool(value)
        return self

    def set_write_only(self, value=True):
        self._write_only = bool(value)
        return self

    def set_observable(self, value=True):
        self._observable = bool(value)
        return self

    def build(self):
        return PropertyAffordance(
            self.name,
            self.forms,
            self.title,
            self.description,
            self._read_only,
            self._write_only,
            self._observable,
            self.json_schema,
        )
