-- A custom source can also be a tool on an MCP server the user connected (ontology/mcp_clients.py): the collector
-- calls the tool on a schedule and records the number it returns.
ALTER TABLE onto_research.custom_sources DROP CONSTRAINT IF EXISTS custom_sources_kind_check;
ALTER TABLE onto_research.custom_sources ADD CONSTRAINT custom_sources_kind_check
    CHECK (kind IN ('rss', 'json', 'csv', 'mcp'));
