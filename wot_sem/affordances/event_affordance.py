from wot_sem.affordances.interaction_affordance import (
    InteractionAffordance,
    InteractionAffordanceBuilder,
)


class EventAffordance(InteractionAffordance):
    def __init__(self, name, forms=None, title=None, description=None, json_schema=None):
        super().__init__(name, forms, title, description, json_schema)

    def __str__(self):
        return f"Event '{self._name}'"


class EventAffordanceBuilder(InteractionAffordanceBuilder):
    def __init__(self, name, forms=None, title=None, description=None):
        super().__init__(name, forms, title, description)

    def build(self):
        return EventAffordance(
            self.name, self.forms, self.title, self.description, self.json_schema
        )
