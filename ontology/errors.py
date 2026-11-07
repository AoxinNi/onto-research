class OntologyError(Exception):
    """A write or query the ontology refuses. The message is meant for the agent to read and fix."""
