from langchain_mcp_adapters.client import MultiServerMCPClient
import yaml
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from datetime import timedelta

UP_DIR = Path(__file__).parent.parent

with open(UP_DIR / 'config.yaml', 'r', encoding='utf-8') as file:
    config = yaml.safe_load(file)

mcp_config = config.get('mcp_server', {})
extensions = config.get('extensions', [])

mcp_server = FastMCP(
    host=mcp_config.get('host', '127.0.0.1'),
    port=mcp_config.get('port', 8000),
    stateless_http=True,
    json_response=True,
)


@mcp_server.tool()
async def health_check() -> str:
    """
    Health check tool. Calling this tool quickly verifies the availability and connectivity of the MCP service. 
    Returns "OK" if the service is running normally.
    """
    return "OK"

# add local tools here


extension_clients = {
    var["name"]: {
        "transport": "streamable_http",
        "url": var["url"],
        "timeout": timedelta(milliseconds=var['timeout']),
    } 
    for var in extensions
}

async def get_tools():
    async with MultiServerMCPClient({
        "mcp_server": {
            "transport": "streamable_http",
            "url": f"http://{mcp_config['host']}:{mcp_config['port']}/mcp",
            "timeout": timedelta(milliseconds=mcp_config['timeout']),
        }, **extension_clients
    }) as client:
        tools = client.get_tools()
        return tools


if __name__ == "__main__":
    mcp_server.run(transport="streamable_http")
