from wot_sem.affordances.interaction_affordance import (
    InteractionAffordance,
    InteractionAffordanceBuilder,
)


class ActionAffordance(InteractionAffordance):
    def __init__(
        self,
        name,
        forms=None,
        title=None,
        description=None,
        safe=False,
        idempotent=False,
        json_schema=None,
    ):
        super().__init__(name, forms, title, description, json_schema)
        self._safe = bool(safe)
        self._idempotent = bool(idempotent)

    @property
    def safe(self):
        return self._safe

    @property
    def idempotent(self):
        return self._idempotent

    def default_form(self):
        return self._forms[0] if self._forms else None

    def forms_for_protocol(self, protocol):
        return [f for f in self._forms if f.protocol and f.protocol.lower() == protocol.lower()]

    def __str__(self):
        descriptor = f"Action '{self._name}'"
        if self._safe:
            descriptor += " (safe)"
        if self._idempotent:
            descriptor += " (idempotent)"
        return descriptor


class ActionAffordanceBuilder(InteractionAffordanceBuilder):
    _safe: bool
    _idempotent: bool

    def __init__(self, name, forms=None, title=None, description=None):
        super().__init__(name, forms, title, description)
        self._safe = False
        self._idempotent = False

    def set_safe(self, safe=True):
        self._safe = bool(safe)
        return self

    def set_idempotent(self, idempotent=True):
        self._idempotent = bool(idempotent)
        return self

    def build(self):
        return ActionAffordance(
            self.name,
            self.forms,
            self.title,
            self.description,
            self._safe,
            self._idempotent,
            self.json_schema,
        )
